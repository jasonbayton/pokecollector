import { describe, expect, it } from 'vitest'

import { initialVariantFor, variantFromFinish } from './recognizedFinish'

const dual = { id: 'a', variants_normal: true, variants_reverse: true }
const holoOnly = { id: 'b', variants_holo: true }
const normalOnly = { id: 'c', variants_normal: true }

describe('variantFromFinish', () => {
  it('maps the physical descriptions the scanner is asked for', () => {
    expect(variantFromFinish('face_foil')).toBe('Reverse Holo')
    expect(variantFromFinish('artwork_foil')).toBe('Holo')
    expect(variantFromFinish('non_foil')).toBe('Normal')
    expect(variantFromFinish('matte')).toBe('Normal')
  })

  it('is null when nothing was read, rather than guessing Normal', () => {
    expect(variantFromFinish(null)).toBeNull()
    expect(variantFromFinish('')).toBeNull()
    expect(variantFromFinish('who knows')).toBeNull()
  })
})

describe('initialVariantFor', () => {
  it('opens on the read finish when the card offers it', () => {
    // The defect: this path filed a read reverse holo as Normal while
    // automatic filing got it right, so the same card landed differently
    // depending on which route was taken.
    expect(initialVariantFor(dual, 'face_foil')).toBe('Reverse Holo')
  })

  it('ignores a finish the card does not offer', () => {
    // A card with no reverse printing must never open on Reverse Holo.
    expect(initialVariantFor(normalOnly, 'face_foil')).toBe('Normal')
  })

  it('never applies First Edition, which is a stamp and not a finish', () => {
    expect(initialVariantFor(dual, 'first_edition')).toBe('Normal')
  })

  it('falls back to the card default when nothing was read', () => {
    // The bystander: the ordinary unread case must behave exactly as before.
    expect(initialVariantFor(dual, null)).toBe('Normal')
    expect(initialVariantFor(holoOnly, null)).toBe('Holo')
  })
})
