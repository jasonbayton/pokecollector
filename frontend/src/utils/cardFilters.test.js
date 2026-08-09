import { describe, expect, it } from 'vitest'

import {
  getCardCategoryLabel,
  getCardSubtypeLabels,
  normalizeCardFilterLabelKey,
  normalizeCardFilterValue,
} from './cardFilters'

describe('getCardSubtypeLabels', () => {
  it('reads the fields a subtype is actually spread across', () => {
    // Reading only `subtypes` made "Item" and "Stage 1" silently miss cards.
    expect(getCardSubtypeLabels({
      subtypes: ['Basic'],
      trainer_type: 'Item',
      energy_type: 'Special',
      stage: 'Stage 1',
    })).toEqual(['Basic', 'Item', 'Special', 'Stage 1'])
  })

  it('does not repeat a value that appears twice', () => {
    expect(getCardSubtypeLabels({ subtypes: ['Item'], trainer_type: 'Item' })).toEqual(['Item'])
  })

  it('survives a card with nothing set', () => {
    expect(getCardSubtypeLabels({})).toEqual([])
    expect(getCardSubtypeLabels(null)).toEqual([])
  })

  it('prefers the canonical spelling so accents do not split an option', () => {
    expect(getCardSubtypeLabels({ subtypes: ['Pokemon Tool'] })).toEqual(['Pokémon Tool'])
  })
})

describe('getCardCategoryLabel', () => {
  it('normalises Pokemon to its accented form', () => {
    expect(getCardCategoryLabel({ supertype: 'Pokemon' })).toBe('Pokémon')
    expect(getCardCategoryLabel({ supertype: 'Pokémon' })).toBe('Pokémon')
  })

  it('is empty when a card has no category', () => {
    expect(getCardCategoryLabel({})).toBe('')
  })
})

describe('normalisation', () => {
  it('matches across accents', () => {
    expect(normalizeCardFilterValue('Pokémon')).toBe('pokemon')
    expect(normalizeCardFilterLabelKey('Pokémon')).toBe('Pokemon')
  })
})
