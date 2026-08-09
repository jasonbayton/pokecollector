"""Recycle bin for manually deleted collection rows.

Scope is deliberately one path: `DELETE /api/collection/{item_id}`. Trades,
sales, trade edits and account deletion are not accidents, they have their own
domain-correct reversal paths, and restoring from here would desynchronise the
product and trade ledgers.
"""

from sqlalchemy import update
from sqlalchemy.orm import Session

from models import Card, CollectionItem, DeletedCollectionItem, User

# Restore is refused rather than guessed at when the world has moved on. These
# codes are stable so the UI can explain why without parsing prose.
BLOCKER_OWNER_MISSING = "owner_missing"
BLOCKER_CARD_MISSING = "card_missing"
BLOCKER_INVALID_QUANTITY = "invalid_quantity"


def archive_collection_item(db: Session, item: CollectionItem, actor: User) -> DeletedCollectionItem:
    """Snapshot a row on its way out.

    Called inside the delete transaction, so either the row is archived and
    deleted together or neither happens.
    """
    entry = DeletedCollectionItem(
        original_collection_item_id=item.id,
        user_id=item.user_id,
        card_id=item.card_id,
        quantity=int(item.quantity or 0),
        condition=item.condition,
        variant=item.variant or "Normal",
        purchase_price=item.purchase_price,
        lang=item.lang,
        grade=getattr(item, "grade", None),
        added_at=item.added_at,
        deleted_by_user_id=actor.id,
        # Stored, not looked up later: the account may be gone by the time
        # anyone asks who deleted the card.
        deleted_by_username=actor.username,
    )
    db.add(entry)
    return entry


def restore_blocker(db: Session, entry: DeletedCollectionItem) -> str | None:
    """Why this entry cannot be restored, or None if it can."""
    if int(entry.quantity or 0) <= 0:
        return BLOCKER_INVALID_QUANTITY
    if not db.query(User.id).filter(User.id == entry.user_id).first():
        return BLOCKER_OWNER_MISSING
    if not entry.card_id or not db.query(Card.id).filter(Card.id == entry.card_id).first():
        return BLOCKER_CARD_MISSING
    return None


def restore_entry(db: Session, entry: DeletedCollectionItem) -> tuple[CollectionItem, str]:
    """Put the row back, and say how.

    Merging uses an atomic UPDATE rather than reading the quantity and writing
    it back. The existing add paths do read-modify-write and race each other
    already; this at least does not add a third writer with the same flaw, and
    it needs no table lock to do it.
    """
    match = (
        db.query(CollectionItem)
        .filter(
            CollectionItem.user_id == entry.user_id,
            CollectionItem.card_id == entry.card_id,
            CollectionItem.variant == entry.variant,
            CollectionItem.condition == entry.condition,
            CollectionItem.lang == entry.lang,
            CollectionItem.purchase_price == entry.purchase_price,
        )
        .first()
    )

    if match:
        db.execute(
            update(CollectionItem)
            .where(CollectionItem.id == match.id)
            .values(quantity=CollectionItem.quantity + int(entry.quantity))
        )
        db.refresh(match)
        return match, "merged"

    restored = CollectionItem(
        card_id=entry.card_id,
        user_id=entry.user_id,
        quantity=int(entry.quantity),
        condition=entry.condition,
        variant=entry.variant or "Normal",
        purchase_price=entry.purchase_price,
        lang=entry.lang,
        added_at=entry.added_at,
    )
    db.add(restored)
    return restored, "recreated"


def serialize_entry(db: Session, entry: DeletedCollectionItem, *, include_owner: bool) -> dict:
    card = db.query(Card).filter(Card.id == entry.card_id).first() if entry.card_id else None
    blocker = restore_blocker(db, entry)
    payload = {
        "id": entry.id,
        "card_id": entry.card_id,
        "card_name": card.name if card else None,
        "set_id": card.set_id if card else None,
        "number": card.number if card else None,
        "images_small": card.images_small if card else None,
        "quantity": entry.quantity,
        "condition": entry.condition,
        "variant": entry.variant,
        "lang": entry.lang,
        "deleted_at": entry.deleted_at,
        "deleted_by": entry.deleted_by_username,
        "restorable": blocker is None,
        "restore_blocker": blocker,
    }
    if include_owner:
        owner = db.query(User).filter(User.id == entry.user_id).first()
        payload["owner"] = owner.username if owner else f"User #{entry.user_id}"
    return payload


def rewrite_archived_card_id(db: Session, old_card_id: str, new_card_id: str) -> int:
    """Follow a custom card that was matched into the catalogue.

    Migration repoints live rows and deletes the old custom card. Without this
    an entry archived beforehand would point at a card that no longer exists,
    and could never be restored.
    """
    if not old_card_id or not new_card_id or old_card_id == new_card_id:
        return 0
    return (
        db.query(DeletedCollectionItem)
        .filter(DeletedCollectionItem.card_id == old_card_id)
        .update({DeletedCollectionItem.card_id: new_card_id}, synchronize_session=False)
    )
