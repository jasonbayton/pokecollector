"""Atomic filing of a scan job's matcher-confirmed cards."""

from __future__ import annotations

import datetime
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Card, CollectionItem, ScanJob, ScanJobItem, User
from services.scan_storage import delete_scan_image
from services.collection_options import ALLOWED_VARIANTS
from services.collection_merge import merge_collection_item
from services.scan_trace import record_variant_decision


DEFAULT_CONDITION = "Mint"

_FINISH_VARIANTS = {
    "normal": "Normal",
    "non foil": "Normal",
    "non-foil": "Normal",
    "nonfoil": "Normal",
    "matte": "Normal",
    "artwork foil": "Holo",
    "artwork_foil": "Holo",
    "foil artwork": "Holo",
    "holo": "Holo",
    "holographic": "Holo",
    "holofoil": "Holo",
    "face foil": "Reverse Holo",
    "face_foil": "Reverse Holo",
    "reverse holo": "Reverse Holo",
    "reverse-holo": "Reverse Holo",
    "reverse holofoil": "Reverse Holo",
    "first edition": "First Edition",
    "first-edition": "First Edition",
    "first_edition": "First Edition",
}


def normalize_recognized_finish(value) -> str | None:
    """Map the model's physical finish description to a collection variant."""
    if not isinstance(value, str):
        return None
    finish = " ".join(value.strip().casefold().replace("_", " ").split())
    if not finish:
        return None
    if finish in _FINISH_VARIANTS:
        return _FINISH_VARIANTS[finish]
    if "first" in finish and "edition" in finish:
        return "First Edition"
    if "reverse" in finish or (
        ("border" in finish or "face" in finish)
        and "foil" in finish
        and ("matte" in finish or "artwork" in finish)
    ):
        return "Reverse Holo"
    if "artwork" in finish and ("foil" in finish or "holo" in finish):
        return "Holo"
    if "non" in finish and ("foil" in finish or "holo" in finish):
        return "Normal"
    return None


def card_offers_variant(card: Card, variant: str) -> bool:
    """Read the catalogue's declared printing flags without inferring one."""
    return bool(getattr(card, {
        "Normal": "variants_normal",
        "Reverse Holo": "variants_reverse",
        "Holo": "variants_holo",
        "First Edition": "variants_first_edition",
    }.get(variant, ""), False))


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


def variant_for_recognized_finish(card: Card, finish) -> str:
    """Use a supported scanned finish, otherwise retain the established default."""
    default = default_variant(card)
    recognized = normalize_recognized_finish(finish)
    # First Edition is a stamp, not a finish. It may be retained in diagnostics
    # when volunteered, but it cannot override the automatic variant choice.
    if recognized in (None, "First Edition") or not card_offers_variant(card, recognized):
        return default
    return recognized if recognized in ALLOWED_VARIANTS else default


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
    recognized_finish=None,
) -> None:
    """Apply the manual add defaults without committing the surrounding job."""
    variant = variant_for_recognized_finish(card, recognized_finish)
    merge_collection_item(
        db, user_id=current_user.id, card_id=card.id, quantity=1,
        condition=DEFAULT_CONDITION, variant=variant, lang=card.lang,
        purchase_price=None,
    )


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
    variant_decisions: list[tuple[int, int, object, str | None, str | None]] = []
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

            # A read finish is used, including when it differs from the
            # default. The default is not a competing observation: it is what
            # is assumed when nothing has been read, so treating a real
            # reading as a disagreement with it would halt on exactly the
            # cards this exists to get right. A reverse holo pulled from a
            # pack is the ordinary case, not an anomaly needing a person.
            #
            # variant_for_recognized_finish still refuses a printing the card
            # does not offer, which is the reading that must never be trusted.
            recognized_finish = (item.recognized or {}).get("finish")
            recognized_variant = normalize_recognized_finish(recognized_finish)
            variant = variant_for_recognized_finish(card, recognized_finish)

            _add_collection_copy(
                db,
                card=card,
                current_user=current_user,
                recognized_finish=recognized_finish,
            )
            variant_decisions.append(
                (current_user.id, item.id, recognized_finish, recognized_variant, variant)
            )
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
    for user_id, item_id, finish, recognized_variant, filed_variant in variant_decisions:
        record_variant_decision(
            user_id,
            job_id,
            item_id,
            recognized_finish=finish,
            recognized_variant=recognized_variant,
            filed_variant=filed_variant,
        )
    return added
