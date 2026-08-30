"""Atomic filing of a scan job's matcher-confirmed cards."""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Card, CollectionItem, ScanJob, ScanJobItem, User
from services.scan_storage import delete_scan_image


DEFAULT_CONDITION = "Mint"


def _positive_price(card: Card, fields: tuple[str, ...]) -> bool:
    """Mirror cardVariants.js: a positive price proves that print exists."""
    for field in fields:
        value = getattr(card, field, None)
        try:
            if float(value) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def default_variant(card: Card) -> str:
    """Server equivalent of frontend getDefaultVariant for automatic adds."""
    normal = bool(card.variants_normal) or _positive_price(card, (
        "price_tcg_normal_low",
        "price_tcg_normal_mid",
        "price_tcg_normal_high",
        "price_tcg_normal_market",
    ))
    if normal:
        return "Normal"

    available = [
        bool(card.variants_reverse) or _positive_price(card, (
            "price_tcg_reverse_low",
            "price_tcg_reverse_mid",
            "price_tcg_reverse_market",
        )),
        bool(card.variants_holo) or _positive_price(card, (
            "price_tcg_holo_low",
            "price_tcg_holo_mid",
            "price_tcg_holo_market",
        )),
        bool(card.variants_first_edition),
    ]
    for exists, variant in zip(available, ("Reverse Holo", "Holo", "First Edition")):
        if exists:
            return variant
    return "Normal"


def _suggested_match(item: ScanJobItem) -> dict | None:
    """Return the one stored candidate selected by the matcher, if still valid."""
    suggested_id = str(item.suggested_match_id or "").strip()
    if not suggested_id:
        return None
    for match in item.matches or []:
        if isinstance(match, dict) and str(match.get("id") or "").strip() == suggested_id:
            return match
    return None


def is_confident_addable(item: ScanJobItem) -> bool:
    return bool(
        not item.resolved
        and item.status == "done"
        and item.identity_confident is True
        and item.suggested_match_id
        and _suggested_match(item)
    )


def confident_addable_count(items: Iterable[ScanJobItem]) -> int:
    return sum(1 for item in items if is_confident_addable(item))


def candidate_ids_to_prepare(items: Iterable[ScanJobItem]) -> set[str]:
    """Identify a best-effort catalogue-preparation set without trusting it to add."""
    ids: set[str] = set()
    for item in items:
        if not (
            not item.resolved
            and item.status == "done"
            and item.identity_confident is True
            and item.suggested_match_id
        ):
            continue
        match = _suggested_match(item)
        if match:
            candidate_id = str(match.get("id") or "").strip()
            if candidate_id:
                ids.add(candidate_id)
    return ids


def _add_collection_copy(
    db: Session,
    *,
    card: Card,
    current_user: User,
) -> None:
    """Apply the manual add defaults without committing the surrounding job."""
    variant = default_variant(card)
    existing = (
        db.query(CollectionItem)
        .filter(
            CollectionItem.card_id == card.id,
            CollectionItem.variant == variant,
            CollectionItem.lang == card.lang,
            CollectionItem.condition == DEFAULT_CONDITION,
            CollectionItem.purchase_price.is_(None),
            CollectionItem.user_id == current_user.id,
            # Only ever joins other unassessed copies. Merging into a row
            # somebody confirmed would extend their statement to a copy nobody
            # looked at; merging into a row that predates the flag would hide
            # this copy from review for ever, since such a row stays unknown.
            CollectionItem.attributes_confirmed.is_(False),
        )
        .with_for_update()
        .first()
    )
    if existing:
        existing.quantity += 1
        # The flag is left exactly as it was. Relabelling an unknown row as
        # automatic would claim to know how its earlier copies got there.
        return
    db.add(CollectionItem(
        card_id=card.id,
        quantity=1,
        # A confident scan identifies the card. It does not establish what
        # condition this copy is in, nor - where a card exists as both - which
        # printing it is. Both are defaults, and the row says so.
        condition=DEFAULT_CONDITION,
        variant=variant,
        purchase_price=None,
        lang=card.lang,
        user_id=current_user.id,
        added_at=datetime.datetime.utcnow(),
        attributes_confirmed=False,
    ))


def add_all_confident_scan_items(
    db: Session,
    *,
    job_id: int,
    current_user: User,
    prepared_card_ids: set[str],
) -> int:
    """File all validated confident scans, or make no change at all.

    Catalogue preparation happens in the API before this function takes a lock.
    This function deliberately trusts neither that preparation snapshot nor the
    prior read of ``matches``: it locks the job and rows, then checks membership
    again before mutating collection or scan state.
    """
    image_paths: list[str | None] = []
    try:
        job = (
            db.query(ScanJob)
            .filter(ScanJob.id == job_id, ScanJob.user_id == current_user.id)
            .with_for_update()
            .first()
        )
        if job is None:
            raise HTTPException(status_code=404, detail="Scan job not found.")

        candidates = (
            db.query(ScanJobItem)
            .filter(
                ScanJobItem.job_id == job.id,
                ScanJobItem.resolved.is_(False),
                ScanJobItem.status == "done",
                ScanJobItem.identity_confident.is_(True),
                ScanJobItem.suggested_match_id.is_not(None),
            )
            .order_by(ScanJobItem.position.asc())
            .with_for_update()
            .all()
        )

        now = datetime.datetime.utcnow()
        added = 0
        for item in candidates:
            match = _suggested_match(item)
            if match is None:
                raise HTTPException(
                    status_code=422,
                    detail="Suggested card is not a stored scan candidate.",
                )
            card_id = str(match.get("id") or "").strip()
            if not card_id or card_id not in prepared_card_ids:
                raise HTTPException(
                    status_code=409,
                    detail="Scan candidates changed while their catalogue cards were prepared.",
                )
            card = db.query(Card).filter(Card.id == card_id).first()
            if card is None:
                raise HTTPException(
                    status_code=409,
                    detail="Prepared catalogue card is no longer available.",
                )

            _add_collection_copy(db, card=card, current_user=current_user)
            image_paths.append(item.image_path)
            item.resolved = True
            item.image_path = None
            item.updated_at = now
            added += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    # Physical removal is deliberately outside the transaction: a rollback
    # leaves the photo for manual review, while a committed resolution removes it.
    for image_path in image_paths:
        delete_scan_image(image_path)
    return added
