import { getAvailableVariants, getDefaultVariant } from './cardVariants'

/**
 * The model's physical description of a card's foil, as a collection variant.
 *
 * Mirrors normalize_recognized_finish in backend/services/scan_bulk_add.py.
 * The scanner is asked where the foil IS rather than to name our variant, so
 * the wording it returns has to be mapped here as well: automatic filing uses
 * the backend's reading, and the review modal has to preselect the same thing
 * or the two paths would file the same card differently.
 */
const EXACT = {
  artwork_foil: 'Holo',
  face_foil: 'Reverse Holo',
  non_foil: 'Normal',
  matte: 'Normal',
}

export function variantFromFinish(finish) {
  if (!finish) return null
  const value = String(finish).trim().toLowerCase().replace(/_/g, ' ').replace(/\s+/g, ' ')
  if (!value) return null
  const key = value.replace(/ /g, '_')
  if (EXACT[key]) return EXACT[key]
  if (value.includes('first') && value.includes('edition')) return 'First Edition'
  if (value.includes('reverse')) return 'Reverse Holo'
  if ((value.includes('border') || value.includes('face')) && value.includes('foil')) return 'Reverse Holo'
  if (value.includes('artwork') && (value.includes('foil') || value.includes('holo'))) return 'Holo'
  if (value.startsWith('non') || value.includes('no foil') || value === 'matte') return 'Normal'
  if (value.includes('holo')) return 'Holo'
  return null
}

/**
 * Which variant the add modal should open on.
 *
 * A reading is used only when the card actually offers that printing. First
 * Edition is a printed stamp rather than a finish, so it is never applied
 * from a reading - the same rule the backend follows.
 */
export function initialVariantFor(card, finish) {
  const recognized = variantFromFinish(finish)
  if (recognized && recognized !== 'First Edition' && getAvailableVariants(card).includes(recognized)) {
    return recognized
  }
  return getDefaultVariant(card)
}
