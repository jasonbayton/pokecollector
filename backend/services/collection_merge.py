"""One concurrency boundary for collection rows with the same identity."""

from __future__ import annotations

import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import CollectionItem


def lock_collection_identities(db: Session, identities: list[dict]) -> None:
    """Serialise collection merges per owner for the transaction.

    Locking each identity is tempting but unsafe when two write paths encounter
    the same identities in different orders (scan position versus request
    order): PostgreSQL can deadlock before either path reaches its row lock.
    Collection writes are infrequent and user-scoped, so one transaction lock
    per owner is the smaller, reliable concurrency boundary. It covers first
    inserts too, where ``FOR UPDATE`` has no row to protect.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    for user_id in sorted({identity["user_id"] for identity in identities}):
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"collection-merge-user:{user_id}"},
        )


def merge_collection_item(
    db: Session,
    *,
    user_id: int,
    card_id: str,
    quantity: int,
    condition: str,
    variant: str,
    lang: str,
    purchase_price,
    grade: str = "raw",
    added_at: datetime.datetime | None = None,
) -> tuple[CollectionItem, bool]:
    """Atomically merge a quantity into its exact collection identity.

    PostgreSQL advisory locks cover the empty-row race. The matching unique
    index installed at startup remains a backstop for future write paths.
    """
    identity = {
        "user_id": user_id,
        "card_id": card_id,
        "condition": condition,
        "variant": variant,
        "lang": lang,
        "purchase_price": purchase_price,
    }
    lock_collection_identities(db, [identity])
    query = db.query(CollectionItem).filter(
        CollectionItem.user_id == user_id,
        CollectionItem.card_id == card_id,
        CollectionItem.condition == condition,
        CollectionItem.variant == variant,
        CollectionItem.lang == lang,
    )
    query = query.filter(
        CollectionItem.purchase_price.is_(None)
        if purchase_price is None
        else CollectionItem.purchase_price == purchase_price
    )
    existing = query.with_for_update().first()
    if existing is not None:
        existing.quantity += quantity
        return existing, False

    item = CollectionItem(
        **identity,
        quantity=quantity,
        grade=grade,
        added_at=added_at or datetime.datetime.utcnow(),
    )
    db.add(item)
    db.flush()
    return item, True
