import { getAvailableVariants, getDefaultVariant } from './cardVariants'
import { cardNumberMatches } from './cardNumbers'
import { TCGDEX_LANGUAGES, normalizeTcgdexLanguage } from './tcgdexLanguages'

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
 * the row filed under the old condition.
 */
export const rowIdentity = row =>
  [row.card.id, row.condition, row.variant, row.lang].join('|')

/**
 * The variant to file this card as, given what the session prefers.
 *
 * A session preference is a preference, not an instruction: a pile sorted by
 * number holds cards with different prints, and a card that has no Normal
 * printing must not be filed as Normal just because the session says so.
 * An empty preference means "whatever each card's own default is", which is
 * the right behaviour for a mixed pile and so the default.
 */
export const resolveVariant = (card, preferred) => {
  if (preferred && getAvailableVariants(card).includes(preferred)) return preferred
  return getDefaultVariant(card)
}

/**
 * The variants worth offering for one card.
 *
 * Falls back to the canonical list when the catalogue advertises none, so a
 * card with missing variant data leaves the user able to choose rather than
 * stuck with an empty menu.
 */
export const variantChoices = (card, canonical) => {
  const available = getAvailableVariants(card)
  return available.length > 0 ? available : canonical
}

/**
 * Languages that have a locally cached printing matching this card number.
 *
 * Rapid entry resolves the requested language from the local catalogue, so
 * offering a language without that printing lets a session fail only at
 * commit time. Keep the picker constrained to choices the server can honour.
 */
export const cachedLanguagesForCard = (card, cards) => {
  const available = new Set(
    cards
      .filter(candidate => cardNumberMatches(candidate.number, card.number))
      .map(candidate => normalizeTcgdexLanguage(candidate.lang)),
  )
  return TCGDEX_LANGUAGES.filter(language => available.has(language.code))
}

export const cachedLanguagesInSet = cards => {
  const available = new Set(cards.map(card => normalizeTcgdexLanguage(card.lang)))
  return TCGDEX_LANGUAGES.filter(language => available.has(language.code))
}

export const newRow = (card, defaults, id) => ({
  id,
  card,
  quantity: 1,
  condition: defaults.condition,
  variant: resolveVariant(card, defaults.variant),
  lang: defaults.lang || card.lang,
  expanded: false,
})

/**
 * Add one copy, merging into the row it already matches.
 *
 * Increments exactly one row. Editing a row's condition can leave two rows
 * sharing an identity, and incrementing every match then counted one entered
 * copy twice.
 */
export const addCopy = (rows, card, defaults, id) => {
  const candidate = newRow(card, defaults, id)
  const identity = rowIdentity(candidate)
  const target = rows.findIndex(row => rowIdentity(row) === identity)
  if (target === -1) return [...rows, candidate]
  return rows.map((row, index) =>
    index === target ? { ...row, quantity: row.quantity + 1 } : row,
  )
}

/**
 * Apply a change to one row, folding it into any row it now duplicates.
 *
 * Editing a row to match another leaves two rows the backend would merge
 * anyway. Coalescing here keeps what the session shows and what gets filed
 * the same thing, rather than showing the same card twice.
 */
export const applyRowChange = (rows, rowId, change) => {
  const edited = rows.map(row => (row.id === rowId ? { ...row, ...change } : row))
  const merged = []
  for (const row of edited) {
    const twin = merged.find(candidate => rowIdentity(candidate) === rowIdentity(row))
    if (twin) twin.quantity += row.quantity
    else merged.push({ ...row })
  }
  return merged
}
