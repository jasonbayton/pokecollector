import { describe, expect, it } from 'vitest'

import { addCopy, applyRowChange, resolveVariant, rowIdentity, variantChoices } from './rapidEntryRows'

const card = { id: 'sv1-25_en', number: '25', lang: 'en', variants_normal: true, variants_reverse: true }
const holoOnly = { id: 'sv1-99_en', number: '99', lang: 'en', variants_holo: true }
const mint = { condition: 'Mint', variant: '', lang: 'en' }
const total = rows => rows.reduce((sum, row) => sum + row.quantity, 0)

describe('addCopy', () => {
  it('merges a repeated number into the row it already matches', () => {
    const rows = addCopy(addCopy([], card, mint, 1), card, mint, 2)
    expect(rows).toHaveLength(1)
    expect(rows[0].quantity).toBe(2)
    expect(rows[0].id).toBe(1)
  })

  it('starts a new row when a session default has changed since the first copy', () => {
    // Changing the condition to NM and re-entering the number used to add to
    // the Mint row, filing the card under a condition the user had moved off.
    const rows = addCopy(addCopy([], card, mint, 1), card, { ...mint, condition: 'NM' }, 2)
    expect(rows).toHaveLength(2)
    expect(rows.map(row => row.condition)).toEqual(['Mint', 'NM'])
    expect(rows.every(row => row.quantity === 1)).toBe(true)
  })

  it('separates rows that differ only by language', () => {
    const rows = addCopy(addCopy([], card, mint, 1), card, { ...mint, lang: 'de' }, 2)
    expect(rows).toHaveLength(2)
  })

  it('counts one entered copy once even when two rows share an identity', () => {
    // Add Mint, add NM, edit the NM row back to Mint, then enter the number
    // again. Incrementing every matching row counted that single copy twice,
    // so the session filed four cards where the user entered three.
    let rows = addCopy([], card, mint, 1)
    rows = addCopy(rows, card, { ...mint, condition: 'NM' }, 2)
    rows = rows.map(row => (row.id === 2 ? { ...row, condition: 'Mint' } : row))
    rows = addCopy(rows, card, mint, 3)
    expect(total(rows)).toBe(3)
  })
})

describe('applyRowChange', () => {
  it('folds a row into the one it now duplicates', () => {
    let rows = addCopy([], card, mint, 1)
    rows = addCopy(rows, card, { ...mint, condition: 'NM' }, 2)
    const merged = applyRowChange(rows, 2, { condition: 'Mint' })
    expect(merged).toHaveLength(1)
    expect(merged[0].quantity).toBe(2)
  })

  it('leaves rows alone when the change creates no duplicate', () => {
    let rows = addCopy([], card, mint, 1)
    rows = addCopy(rows, card, { ...mint, condition: 'NM' }, 2)
    const changed = applyRowChange(rows, 2, { condition: 'LP' })
    expect(changed).toHaveLength(2)
    expect(total(changed)).toBe(2)
  })
})

describe('resolveVariant', () => {
  it('does not file a card as a print it does not have', () => {
    // A session set to Normal must not file a holo-only card as Normal.
    expect(resolveVariant(holoOnly, 'Normal')).toBe('Holo')
  })

  it('honours a session preference the card actually offers', () => {
    expect(resolveVariant(card, 'Reverse Holo')).toBe('Reverse Holo')
  })

  it('falls back to the card default when no preference is set', () => {
    expect(resolveVariant(card, '')).toBe('Normal')
    expect(resolveVariant(holoOnly, '')).toBe('Holo')
  })
})

describe('variantChoices', () => {
  it('offers only the prints the card has', () => {
    expect(variantChoices(card, ['Normal', 'Holo'])).toEqual(['Normal', 'Reverse Holo'])
  })

  it('falls back to the canonical list when the catalogue advertises none', () => {
    expect(variantChoices({ id: 'x' }, ['Normal', 'Holo'])).toEqual(['Normal', 'Holo'])
  })
})

describe('rowIdentity', () => {
  it('is the four fields the backend merges on', () => {
    expect(rowIdentity({ card, condition: 'NM', variant: 'Holo', lang: 'fr' }))
      .toBe('sv1-25_en|NM|Holo|fr')
  })
})
