import { describe, expect, it } from 'vitest'
import {
  GRID_PRESETS,
  MAX_GRID_DIMENSION,
  binderIsMapped,
  clampPage,
  gridIsValid,
  pageAndPocket,
  pageCells,
  pocketsPerPage,
  positionOf,
  rowAndColumn,
} from './binderSlots'

describe('binder grid validity', () => {
  it('accepts an absent grid, meaning the binder is not mapped', () => {
    expect(gridIsValid(null, null)).toBe(true)
    expect(binderIsMapped({ grid_rows: null, grid_columns: null })).toBe(false)
  })

  it('rejects half a grid, which leaves pocket numbering undefined', () => {
    expect(gridIsValid(3, null)).toBe(false)
    expect(gridIsValid(null, 3)).toBe(false)
  })

  it('enforces the same bounds as the server', () => {
    expect(gridIsValid(1, 1)).toBe(true)
    expect(gridIsValid(MAX_GRID_DIMENSION, MAX_GRID_DIMENSION)).toBe(true)
    expect(gridIsValid(0, 3)).toBe(false)
    expect(gridIsValid(MAX_GRID_DIMENSION + 1, 3)).toBe(false)
    expect(gridIsValid(2.5, 3)).toBe(false)
  })

  it('offers the pocket pages people actually own', () => {
    expect(GRID_PRESETS.every(({ rows, columns }) => gridIsValid(rows, columns))).toBe(true)
  })
})

describe('pocket arithmetic', () => {
  it('reads left to right, then top to bottom', () => {
    expect(rowAndColumn(1, 3)).toEqual({ row: 1, column: 1 })
    expect(rowAndColumn(3, 3)).toEqual({ row: 1, column: 3 })
    expect(rowAndColumn(4, 3)).toEqual({ row: 2, column: 1 })
    expect(rowAndColumn(9, 3)).toEqual({ row: 3, column: 3 })
  })

  it('handles a non-square page', () => {
    expect(pocketsPerPage(3, 4)).toBe(12)
    expect(rowAndColumn(5, 4)).toEqual({ row: 2, column: 1 })
  })

  it('round trips between an absolute position and a page pocket', () => {
    for (const position of [1, 9, 10, 37, 9999]) {
      const { page, pocket } = pageAndPocket(position, 3, 3)
      expect(positionOf(page, pocket, 3, 3)).toBe(position)
    }
  })

  it('puts the first pocket of page two straight after the last of page one', () => {
    expect(pageAndPocket(9, 3, 3)).toEqual({ page: 1, pocket: 9 })
    expect(pageAndPocket(10, 3, 3)).toEqual({ page: 2, pocket: 1 })
  })
})

describe('page navigation', () => {
  it('clamps rather than throwing, so a stale page still renders', () => {
    expect(clampPage(5, 3)).toBe(3)
    expect(clampPage(0, 3)).toBe(1)
    expect(clampPage(-1, 3)).toBe(1)
    expect(clampPage(2, 3)).toBe(2)
  })

  it('treats an empty binder as having one page', () => {
    expect(clampPage(1, 0)).toBe(1)
    expect(clampPage(4, null)).toBe(1)
  })
})

describe('page cells', () => {
  const response = {
    page: 1,
    page_count: 1,
    grid_rows: 3,
    grid_columns: 3,
    placed_total: 2,
    pockets: [
      { pocket: 1, binder_card_id: 11 },
      { pocket: 5, binder_card_id: 22 },
    ],
  }

  it('generates every pocket, not only the occupied ones', () => {
    const cells = pageCells(response)
    expect(cells).toHaveLength(9)
    expect(cells.filter(cell => cell.isEmpty)).toHaveLength(7)
  })

  it('places each card in the pocket the server reported', () => {
    const cells = pageCells(response)
    expect(cells.find(cell => cell.pocket === 1).binderCardId).toBe(11)
    expect(cells.find(cell => cell.pocket === 5).binderCardId).toBe(22)
    expect(cells.find(cell => cell.pocket === 2).binderCardId).toBeNull()
  })

  it('gives every cell its row and column for layout', () => {
    const cells = pageCells(response)
    expect(cells.find(cell => cell.pocket === 5)).toMatchObject({ row: 2, column: 2 })
  })

  it('survives an absent response rather than rendering a broken grid', () => {
    expect(pageCells(null)).toEqual([])
  })
})
