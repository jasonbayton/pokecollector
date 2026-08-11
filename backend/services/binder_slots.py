"""Placement of physical copies into binder pockets.

Every mutation here assumes the caller already holds a row lock on the binder,
which is the pattern the binder endpoints already follow. Concurrency is
otherwise handled by the database: pocket uniqueness is a constraint, not a
check, so two people placing into the same pocket at once produces one success
and one integrity error rather than a lost placement.
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Binder, BinderCard, BinderSlot
from services.binder_allocations import stored_binder_quantity
from services.binder_layout import (
    MAX_LAYOUT_SLOTS,
    grid_is_valid,
    page_and_pocket,
    pockets_per_page,
    position_of,
)


class SlotError(Exception):
    """A placement that cannot be satisfied. Carries a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def binder_is_mapped(binder: Binder) -> bool:
    return binder.grid_rows is not None and binder.grid_columns is not None


def require_mapped(binder: Binder) -> tuple[int, int]:
    if not binder_is_mapped(binder):
        raise SlotError("not_mapped", "This binder has no page layout configured.")
    return binder.grid_rows, binder.grid_columns


def slot_count(db: Session, binder_id: int) -> int:
    return db.query(func.count(BinderSlot.id)).filter(
        BinderSlot.binder_id == binder_id
    ).scalar() or 0


def entry_slot_count(db: Session, binder_card_id: int) -> int:
    return db.query(func.count(BinderSlot.id)).filter(
        BinderSlot.binder_card_id == binder_card_id
    ).scalar() or 0


def validate_position(binder: Binder, page: int, pocket: int) -> None:
    rows, columns = require_mapped(binder)
    if page < 1:
        raise SlotError("bad_position", "Page numbers start at 1.")
    per_page = pockets_per_page(rows, columns)
    if pocket < 1 or pocket > per_page:
        raise SlotError(
            "bad_position",
            f"This binder has {per_page} pockets per page.",
        )
    if position_of(page, pocket, rows, columns) > MAX_LAYOUT_SLOTS:
        raise SlotError(
            "layout_full",
            f"A binder layout is limited to {MAX_LAYOUT_SLOTS} pockets.",
        )


def occupant(db: Session, binder_id: int, page: int, pocket: int) -> BinderSlot | None:
    return db.query(BinderSlot).filter(
        BinderSlot.binder_id == binder_id,
        BinderSlot.page == page,
        BinderSlot.pocket == pocket,
    ).first()


def first_free_positions(db: Session, binder: Binder, count: int) -> list[tuple[int, int]]:
    """The lowest unoccupied pockets, in physical order.

    Used when placements are created without the user choosing a pocket, such as
    enabling a layout on a binder that already has entries, or importing a CSV
    that carries no positions.
    """
    rows, columns = require_mapped(binder)
    taken = {
        position_of(page, pocket, rows, columns)
        for page, pocket in db.query(BinderSlot.page, BinderSlot.pocket).filter(
            BinderSlot.binder_id == binder.id
        ).all()
    }
    found: list[tuple[int, int]] = []
    position = 1
    while len(found) < count:
        if position > MAX_LAYOUT_SLOTS:
            raise SlotError(
                "layout_full",
                f"A binder layout is limited to {MAX_LAYOUT_SLOTS} pockets.",
            )
        if position not in taken:
            found.append(page_and_pocket(position, rows, columns))
        position += 1
    return found


def place(db: Session, binder: Binder, entry: BinderCard, page: int, pocket: int) -> BinderSlot:
    """Put one copy of an entry into a specific pocket.

    Refuses rather than silently overwriting when the pocket is taken; moving an
    existing placement is a separate, explicit operation.
    """
    validate_position(binder, page, pocket)
    if entry_slot_count(db, entry.id) >= stored_binder_quantity(entry.required_quantity):
        raise SlotError(
            "entry_fully_placed",
            "Every copy of this entry already has a pocket.",
        )
    if slot_count(db, binder.id) >= MAX_LAYOUT_SLOTS:
        raise SlotError(
            "layout_full",
            f"A binder layout is limited to {MAX_LAYOUT_SLOTS} pockets.",
        )
    if occupant(db, binder.id, page, pocket) is not None:
        raise SlotError("pocket_taken", "That pocket already holds a card.")

    slot = BinderSlot(
        binder_card_id=entry.id, binder_id=binder.id, page=page, pocket=pocket
    )
    db.add(slot)
    db.flush()
    return slot


def move(db: Session, binder: Binder, slot: BinderSlot, page: int, pocket: int) -> None:
    """Move a placement, swapping with whatever already sits in the target.

    A swap exchanges which entry each pocket holds rather than moving the two
    rows past each other. Moving positions would need a temporary value to dodge
    the binder-wide uniqueness constraint, and there is no legal one to park in:
    page and pocket are both constrained to be positive, and any real position
    used as scratch space could collide with a concurrent swap.

    Because the rows stay put, placed_at describes when a pocket was last filled
    rather than how long a particular copy has sat there, which is the more
    useful reading for a physical binder anyway.
    """
    validate_position(binder, page, pocket)
    if slot.page == page and slot.pocket == pocket:
        return

    other = occupant(db, binder.id, page, pocket)
    if other is None:
        slot.page, slot.pocket = page, pocket
        db.flush()
        return

    slot.binder_card_id, other.binder_card_id = other.binder_card_id, slot.binder_card_id
    db.flush()


def reconcile_entry(db: Session, entry: BinderCard) -> int:
    """Drop placements an entry no longer has copies for.

    Reducing required_quantity leaves surplus slots behind. The highest
    positions go first, so the copies a user placed deliberately at the front of
    the binder survive. Returns how many were removed.
    """
    wanted = stored_binder_quantity(entry.required_quantity)
    slots = db.query(BinderSlot).filter(
        BinderSlot.binder_card_id == entry.id
    ).order_by(BinderSlot.page.desc(), BinderSlot.pocket.desc()).all()
    surplus = len(slots) - wanted
    if surplus <= 0:
        return 0
    for slot in slots[:surplus]:
        db.delete(slot)
    db.flush()
    return surplus


def merge_binder_cards(db: Session, source: BinderCard, target: BinderCard, combined_quantity: int) -> None:
    """Absorb one entry into another, keeping the physical placements.

    The three callers that merge entries - the print optimiser, the manual card
    switch and custom card promotion - all previously set the surviving
    quantity and deleted the source row. With slots cascading from the parent,
    that silently destroyed the source's placements: the copies stayed in the
    binder but the app forgot where they were.

    Sharing one helper is deliberate. Three separate copies of this logic is
    exactly how one of them ends up not reparenting.
    """
    target.required_quantity = combined_quantity
    db.query(BinderSlot).filter(
        BinderSlot.binder_card_id == source.id
    ).update(
        {"binder_card_id": target.id, "binder_id": target.binder_id},
        synchronize_session=False,
    )
    db.flush()
    db.delete(source)
    db.flush()
    reconcile_entry(db, target)


def page_view(db: Session, binder: Binder, page: int) -> dict:
    """The grid for one page, with occupied pockets filled in.

    Empty pockets are generated rather than stored, so a binder costs nothing
    until cards are actually placed in it.
    """
    rows, columns = require_mapped(binder)
    per_page = pockets_per_page(rows, columns)
    slots = db.query(BinderSlot).filter(
        BinderSlot.binder_id == binder.id, BinderSlot.page == page
    ).all()
    by_pocket = {slot.pocket: slot for slot in slots}
    total_slots = slot_count(db, binder.id)
    highest = db.query(func.max(BinderSlot.page)).filter(
        BinderSlot.binder_id == binder.id
    ).scalar() or 1
    # Placements per entry across the whole binder, not just this page. The
    # client needs it to know what is still unplaced: counting only the visible
    # page makes a card placed on page one look unplaced from page two.
    placed_by_entry = dict(
        db.query(BinderSlot.binder_card_id, func.count(BinderSlot.id))
        .filter(BinderSlot.binder_id == binder.id)
        .group_by(BinderSlot.binder_card_id)
        .all()
    )
    return {
        "page": page,
        "page_count": max(highest, 1),
        "placed_by_entry": [
            {"binder_card_id": entry_id, "placed": count}
            for entry_id, count in sorted(placed_by_entry.items())
        ],
        "grid_rows": rows,
        "grid_columns": columns,
        "placed_total": total_slots,
        "pockets": [
            {
                "pocket": pocket,
                "binder_card_id": by_pocket[pocket].binder_card_id if pocket in by_pocket else None,
            }
            for pocket in range(1, per_page + 1)
        ],
    }
