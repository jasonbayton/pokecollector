import { describe, expect, it } from 'vitest'

import { manualSearchParams, tcgCardIdFrom, toManualScanMatch } from './scanManualPickerHelpers'

// The shape GET /api/cards/search actually returns, per _card_to_dict in
// backend/api/cards.py: set_ref and images_small, no tcg_card_id, no
// set_abbreviation, no image.
const catalogueCard = {
  id: 'base1-64_en',
  name: 'Pikachu',
  number: '64',
  set_id: 'base1',
  set_ref: { id: 'base1_en', tcg_set_id: 'base1', name: 'Base Set', abbreviation: 'BS', lang: 'en' },
  images_small: 'https://example.test/small.png',
  images_large: 'https://example.test/large.png',
}

describe('toManualScanMatch', () => {
  it('supplies the tcg_card_id the resolve call reports as the correction', () => {
    // Without this the modal reported undefined, the server saw no card_id,
    // and the correction was never recorded even though the card was added.
    expect(toManualScanMatch(catalogueCard).tcg_card_id).toBe('base1-64')
  })

  it('supplies the set code both the picker and the confirm modal display', () => {
    expect(toManualScanMatch(catalogueCard).set_abbreviation).toBe('BS')
  })

  it('falls back to the set id when the catalogue has no abbreviation', () => {
    const card = { ...catalogueCard, set_ref: { ...catalogueCard.set_ref, abbreviation: null } }
    expect(toManualScanMatch(card).set_abbreviation).toBe('base1')
  })

  it('supplies an image from the catalogue field names', () => {
    expect(toManualScanMatch(catalogueCard).image).toBe('https://example.test/small.png')
  })

  it('carries the language through from the set', () => {
    expect(toManualScanMatch(catalogueCard).lang).toBe('en')
  })

  it('leaves a value the card already carries alone', () => {
    const candidate = { id: 'x_en', tcg_card_id: 'already', set_abbreviation: 'SET', image: 'i', lang: 'de' }
    expect(toManualScanMatch(candidate)).toMatchObject({
      tcg_card_id: 'already', set_abbreviation: 'SET', image: 'i', lang: 'de',
    })
  })
})

describe('tcgCardIdFrom', () => {
  it('strips a language suffix', () => {
    expect(tcgCardIdFrom('sv1-25_fr')).toBe('sv1-25')
  })

  it('leaves a trailing segment that is not a language alone', () => {
    // A card id whose last segment is part of the number must survive intact.
    expect(tcgCardIdFrom('sv1-25_promo')).toBe('sv1-25_promo')
  })

  it('handles an id with no suffix at all', () => {
    expect(tcgCardIdFrom('sv1-25')).toBe('sv1-25')
  })
})

describe('manualSearchParams', () => {
  it('sends the trimmed query and language the catalogue search expects', () => {
    expect(manualSearchParams('  SVI 25 ', 'de')).toEqual({ name: 'SVI 25', lang: 'de', page: 1, page_size: 20 })
  })
})
