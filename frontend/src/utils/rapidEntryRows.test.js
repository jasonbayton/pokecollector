import { describe, expect, it } from 'vitest'

import { addCopy, rowIdentity } from './rapidEntryRows'

const card = { id: 'sv1-25_en', number: '25', lang: 'en', variants_normal: true }
const mint = { condition: 'Mint', variant: 'Normal', lang: 'en' }

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

  it('gives every row an id of its own so editing one leaves the rest alone', () => {
    const rows = addCopy(addCopy([], card, mint, 1), card, { ...mint, variant: 'Holo' }, 2)
    expect(rows.map(row => row.id)).toEqual([1, 2])
  })
})

describe('rowIdentity', () => {
  it('is the four fields the backend merges on', () => {
    expect(rowIdentity({ card, condition: 'NM', variant: 'Holo', lang: 'fr' }))
      .toBe('sv1-25_en|NM|Holo|fr')
  })
})
