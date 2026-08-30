import { getDefaultVariant } from './cardVariants'

/**
 * Row bookkeeping for a rapid entry session.
 *
 * Kept out of the component so it can be tested directly: the vitest suite
 * runs in a node environment with no DOM, so effects and state updates are
 * not exercised, and logic that only exists inside a component is logic
 * nothing checks.
 */

/**
 * What makes two session rows the same collection row.
 *
 * The backend merges on card, condition, variant and language, so the session
 * has to group by the same four things. Grouping on the card alone meant that
 * changing the session's condition and re-entering a number quietly added to
 * the row filed under the old condition instead of starting a new one.
 */
export const rowIdentity = row =>
  [row.card.id, row.condition, row.variant, row.lang].join('|')

export const newRow = (card, defaults, id) => ({
  id,
  card,
  quantity: 1,
  condition: defaults.condition,
  variant: defaults.variant || getDefaultVariant(card),
  lang: defaults.lang || card.lang,
  expanded: false,
})

/**
 * Add one copy, merging into a row that matches on all four fields.
 *
 * Returns the rows unchanged in identity order, so a repeated number keeps
 * its place in the list rather than jumping to the end.
 */
export const addCopy = (rows, card, defaults, id) => {
  const candidate = newRow(card, defaults, id)
  const identity = rowIdentity(candidate)
  if (rows.some(row => rowIdentity(row) === identity)) {
    return rows.map(row =>
      rowIdentity(row) === identity ? { ...row, quantity: row.quantity + 1 } : row,
    )
  }
  return [...rows, candidate]
}
