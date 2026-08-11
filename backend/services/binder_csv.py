from services.quantity_limits import MAX_CARD_QUANTITY

BINDER_CSV_DUPLICATE_QUANTITY_ERROR = (
    f"combined required_quantity for duplicate card must be between 1 and {MAX_CARD_QUANTITY}"
)


def combine_binder_required_quantity(current_quantity: int, incoming_quantity: int) -> int:
    """Combine duplicate wishlist/deck binder CSV quantities.

    Duplicate rows that intentionally represent multiple copies are summed only
    while the combined import quantity stays within the same per-row limit the
    rest of the app enforces. The bound is taken from the shared constant rather
    than restated, because this file kept its own copy of the old value and went
    on rejecting imports everything else had started accepting.
    """
    combined_quantity = current_quantity + incoming_quantity
    if combined_quantity > MAX_CARD_QUANTITY:
        raise ValueError(BINDER_CSV_DUPLICATE_QUANTITY_ERROR)
    return combined_quantity
