"""Geometry for binders mapped to a physical album.

A mapped binder records which page and which pocket each copy sits in, so a card
can be found on the shelf rather than only known to be owned. Empty pockets are
never stored: the grid for a page is generated on read and occupied slots are
joined onto it, which keeps resizing free and gaps costless.
"""

# Bounds for a page's grid. Chosen to cover every common pocket page (2x2, 3x3,
# 4x3, 3x4) with room for unusual ones, while keeping a single rendered page
# small enough that it never needs virtualising.
MIN_GRID_DIMENSION = 1
MAX_GRID_DIMENSION = 12

# Total pockets one binder may map. At nine to a page this is over eleven
# hundred pages, far past any physical album, but it bounds the work any single
# placement or audit query can be asked to do.
MAX_LAYOUT_SLOTS = 9999

# Grids offered as presets in the UI. Free-form dimensions remain available
# within the bounds above; these are simply the ones people actually own.
GRID_PRESETS = ((2, 2), (3, 3), (3, 4), (4, 3))


def grid_is_valid(rows: int | None, columns: int | None) -> bool:
    """Whether a grid is either absent entirely or fully and sensibly specified.

    Both dimensions move together: a binder is mapped or it is not, and a half
    configured grid would leave pocket numbering undefined.
    """
    if rows is None and columns is None:
        return True
    if rows is None or columns is None:
        return False
    return all(
        MIN_GRID_DIMENSION <= value <= MAX_GRID_DIMENSION for value in (rows, columns)
    )


def pockets_per_page(rows: int, columns: int) -> int:
    return rows * columns


def position_of(page: int, pocket: int, rows: int, columns: int) -> int:
    """Absolute 1-based position of a pocket, used for ordering and capacity."""
    return (page - 1) * pockets_per_page(rows, columns) + pocket


def page_and_pocket(position: int, rows: int, columns: int) -> tuple[int, int]:
    """Inverse of position_of, for walking a binder in physical order."""
    per_page = pockets_per_page(rows, columns)
    return (position - 1) // per_page + 1, (position - 1) % per_page + 1


def row_and_column(pocket: int, columns: int) -> tuple[int, int]:
    """Where a pocket sits on the page, reading left to right, top to bottom."""
    return (pocket - 1) // columns + 1, (pocket - 1) % columns + 1
