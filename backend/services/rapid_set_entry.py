"""Atomic collection writes for a prepared set checklist session."""

from __future__ import annotations

from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Card, Set, User
from services.card_numbers import card_number_matches
from services.collection_options import ALLOWED_CONDITIONS, ALLOWED_VARIANTS
from services.collection_merge import lock_collection_identities, merge_collection_item
from services.tcgdex_languages import is_supported_tcgdex_language


def _error(index: int, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"index": index, "message": message})


def prepare_rapid_set_entry(db: Session, *, set_id: str, items: list) -> list[dict]:
    """Resolve a session's rows against the locally cached checklist.

    Deliberately does no catalogue fetches. The browser can only submit IDs
    from the checklist it already loaded, so a rapid session costs no upstream
    requests however many cards it files - which is the whole point of the
    mode, and what /collection/bulk-add could not offer.

    Runs inside the write transaction rather than against the browser's
    snapshot, so a card that left the set between loading the checklist and
    filing it is rejected instead of written.
    """
    set_row = db.query(Set).filter(Set.id == set_id).first()
    if set_row is None:
        raise HTTPException(status_code=404, detail="Set not found.")
    expected_set_id = set_row.tcg_set_id or set_row.id
    card_ids = {item.card_id for item in items}
    cards = {
        card.id: card
        for card in db.query(Card).filter(Card.id.in_(card_ids)).all()
    }
    requested_languages = {item.lang for item in items}
    cards_by_language: dict[str, list[Card]] = defaultdict(list)
    for card in db.query(Card).filter(
        Card.set_id == expected_set_id,
        Card.lang.in_(requested_languages),
    ).all():
        cards_by_language[card.lang].append(card)

    prepared: list[dict] = []
    for index, item in enumerate(items):
        if item.condition not in ALLOWED_CONDITIONS:
            raise _error(index, "condition is not supported")
        if item.variant not in ALLOWED_VARIANTS:
            raise _error(index, "variant is not supported")
        card = cards.get(item.card_id)
        if card is None or card.set_id != expected_set_id:
            raise _error(index, "card is not in this set")
        if not is_supported_tcgdex_language(item.lang):
            raise _error(index, "language is not supported")
        target = next(
            (candidate for candidate in cards_by_language[item.lang]
             if card_number_matches(candidate.number, card.number)),
            None,
        )
        if target is None:
            raise _error(index, "card is not cached in the selected language")
        prepared.append({
            "card_id": target.id,
            "quantity": item.quantity,
            "condition": item.condition,
            "variant": item.variant,
            "lang": item.lang,
        })
    return prepared


def commit_rapid_set_entry(db: Session, *, set_id: str, items: list, current_user: User) -> dict:
    """Commit a prepared session in one transaction, locking every merged row."""
    prepared = list(items)
    try:
        # A rollback releases any read transaction still open before PostgreSQL
        # takes the write locks below, matching the scan bulk path.
        db.rollback()
        prepared_rows = prepare_rapid_set_entry(db, set_id=set_id, items=prepared)

        combined: dict[tuple[str, str, str, str], int] = defaultdict(int)
        for item in prepared_rows:
            combined[(item["card_id"], item["condition"], item["variant"], item["lang"])] += item["quantity"]

        added = 0
        updated = 0
        # Locks are taken in a fixed order rather than the order the browser
        # happened to send. Two sessions filing the same cards in different
        # orders would otherwise be able to deadlock, each holding the row the
        # other is waiting for. The scan path orders its locks for the same
        # reason, by ScanJobItem.position.
        ordered = sorted(combined.items())
        lock_collection_identities(db, [
            {
                "user_id": current_user.id,
                "card_id": card_id,
                "condition": condition,
                "variant": variant,
                "lang": lang,
                "purchase_price": None,
            }
            for (card_id, condition, variant, lang), _quantity in ordered
        ])
        for (card_id, condition, variant, lang), quantity in ordered:
            _, created = merge_collection_item(
                db, card_id=card_id, quantity=quantity, condition=condition,
                variant=variant, purchase_price=None, lang=lang, user_id=current_user.id,
            )
            if created:
                added += 1
            else:
                updated += 1
        db.commit()
        return {"added": added, "updated": updated, "quantity": sum(combined.values())}
    except Exception:
        db.rollback()
        raise
