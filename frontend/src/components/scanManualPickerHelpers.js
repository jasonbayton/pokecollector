import { TCGDEX_LANGUAGES } from '../utils/tcgdexLanguages'

const LANG_CODES = new Set(TCGDEX_LANGUAGES.map(language => language.code || language))

export function manualSearchParams(query, lang) {
  return {
    name: query.trim(),
    lang,
    page: 1,
    page_size: 20,
  }
}

/**
 * The catalogue's id carries the language, as in "base1-64_en".
 *
 * The scan pipeline identifies a printing by the language-free id, which is
 * what a candidate's tcg_card_id holds and what the resolve endpoint records
 * as the correction. Only strip a suffix that is actually a language code:
 * a card numbered "sv1-25_promo" must not lose its last segment.
 */
export function tcgCardIdFrom(cardId) {
  const id = String(cardId || '')
  const cut = id.lastIndexOf('_')
  if (cut === -1) return id
  return LANG_CODES.has(id.slice(cut + 1)) ? id.slice(0, cut) : id
}

/**
 * Map a catalogue card onto the shape the scan add modal reads.
 *
 * These two shapes are NOT the same, which is what makes this mapping load
 * bearing rather than ceremonial. A catalogue card has set_ref and
 * images_small; a scan candidate has set_abbreviation and image. Most
 * importantly a catalogue card has no tcg_card_id at all, and the modal
 * reports that field back as the confirmed card. Passing the card through
 * unchanged left it undefined, so the card was added to the collection but
 * the correction was never recorded - silently losing exactly the
 * "the right card was never retrieved" evidence this picker exists to
 * capture.
 */
export function toManualScanMatch(card) {
  return {
    ...card,
    tcg_card_id: card.tcg_card_id || tcgCardIdFrom(card.id),
    set_abbreviation: card.set_abbreviation || card.set_ref?.abbreviation || card.set_id || '',
    image: card.image || card.images_small || card.images_large || null,
    lang: card.lang || card.set_ref?.lang || null,
  }
}
