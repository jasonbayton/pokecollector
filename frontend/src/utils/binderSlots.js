// Pocket arithmetic for a binder mapped to a physical album.
//
// Mirrors services/binder_layout.py. Kept as plain functions so the page grid
// can be laid out and navigated without a round trip, and so the maths is
// testable on its own rather than only through a rendered component.

export const MIN_GRID_DIMENSION = 1
export const MAX_GRID_DIMENSION = 12
export const MAX_LAYOUT_SLOTS = 9999

export const GRID_PRESETS = [
  { rows: 2, columns: 2 },
  { rows: 3, columns: 3 },
  { rows: 3, columns: 4 },
  { rows: 4, columns: 3 },
]

export function pocketsPerPage(rows, columns) {
  return rows * columns
}

export function gridIsValid(rows, columns) {
  if (rows == null && columns == null) return true
  if (rows == null || columns == null) return false
  return [rows, columns].every(
    value => Number.isInteger(value) && value >= MIN_GRID_DIMENSION && value <= MAX_GRID_DIMENSION,
  )
}

export function binderIsMapped(binder) {
  return Boolean(binder?.grid_rows && binder?.grid_columns)
}

// Where a pocket sits on the page, reading left to right and top to bottom.
export function rowAndColumn(pocket, columns) {
  return {
    row: Math.floor((pocket - 1) / columns) + 1,
    column: ((pocket - 1) % columns) + 1,
  }
}

export function positionOf(page, pocket, rows, columns) {
  return (page - 1) * pocketsPerPage(rows, columns) + pocket
}

export function pageAndPocket(position, rows, columns) {
  const perPage = pocketsPerPage(rows, columns)
  return {
    page: Math.floor((position - 1) / perPage) + 1,
    pocket: ((position - 1) % perPage) + 1,
  }
}

// Page navigation clamps rather than throwing, so a stale page number from a
// deleted placement lands somewhere valid instead of rendering nothing.
export function clampPage(page, pageCount) {
  const total = Math.max(1, pageCount || 1)
  if (!Number.isInteger(page) || page < 1) return 1
  return Math.min(page, total)
}

// The grid for a page, with occupied pockets filled in. Empty pockets are
// generated here rather than fetched, matching the server, which stores only
// the pockets that actually hold a card.
export function pageCells(pageResponse) {
  if (!pageResponse) return []
  const { grid_rows: rows, grid_columns: columns, pockets = [] } = pageResponse
  const occupied = new Map(pockets.map(p => [p.pocket, p.binder_card_id]))
  return Array.from({ length: pocketsPerPage(rows, columns) }, (_, index) => {
    const pocket = index + 1
    const binderCardId = occupied.get(pocket) ?? null
    return { pocket, binderCardId, isEmpty: binderCardId == null, ...rowAndColumn(pocket, columns) }
  })
}
