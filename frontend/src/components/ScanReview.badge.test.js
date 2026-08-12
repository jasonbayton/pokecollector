import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ScanItemPanel } from './ScanReview'

// CardDisplay is stubbed down to the one thing this test needs to read: which
// candidate a piece of markup belongs to. It still renders the overlay it is
// handed, because that is where the badge lives - a stub that dropped the
// overlay would report every card as unbadged and pass on a broken component.
//
// The marker is card.id rather than card.tcg_card_id because a card id is not
// unique within a candidate list: the same card appears once per language
// searched. Marking by tcg_card_id would make the two rows of the duplicate
// fixture below indistinguishable, which is precisely the bug that fixture is
// here to catch.
vi.mock('./card-system', () => ({
  CardDisplay: ({ card, overlay }) => createElement(
    'div',
    { 'data-candidate': card.id },
    overlay,
  ),
}))

vi.mock('../api/client', () => ({
  fetchScanJobItemImage: vi.fn(() => new Promise(() => {})),
}))

const t = key => key

const matches = [
  { id: 'base1-58_en', tcg_card_id: 'base1-58', name: 'Pikachu', lang: 'en' },
  { id: 'base1-59_en', tcg_card_id: 'base1-59', name: 'Pikachu', lang: 'en' },
]

// One card, listed twice because the matcher searched two languages. The
// searches are per language and the candidate dedup keys on the per-language
// id, so this shape is what the backend really produces for any non-English
// scan of a card that also exists in the English catalogue.
const sameCardTwice = [
  { id: 'base1-4_de', tcg_card_id: 'base1-4', name: 'Glurak', lang: 'de' },
  { id: 'base1-4_en', tcg_card_id: 'base1-4', name: 'Charizard', lang: 'en' },
]

const baseItem = {
  id: 7,
  position: 0,
  status: 'done',
  has_image: false,
  recognized: { name: 'Pikachu', number: '58' },
  matches,
  identity_confident: null,
  identity_decision: null,
  suggested_match_id: null,
}

const render = (item = {}) => renderToStaticMarkup(createElement(ScanItemPanel, {
  jobId: 1,
  item: { ...baseItem, ...item },
  onAdd: () => {},
  onRetry: () => {},
  onDismiss: () => {},
  t,
}))

// Splitting on the candidate marker gives one chunk per card, each running to
// the start of the next card, so a badge is attributed to the card it is
// actually inside. Counting badges alone would not notice one on the wrong card.
function candidateChunks(markup) {
  return markup.split('data-candidate="').slice(1).map(chunk => ({
    id: chunk.slice(0, chunk.indexOf('"')),
    markup: chunk,
  }))
}

function badgedCandidates(markup) {
  return candidateChunks(markup)
    .filter(chunk => chunk.markup.includes('scanner.suggestedMatch'))
    .map(chunk => chunk.id)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ScanItemPanel suggested match', () => {
  it('badges the candidate the matcher actually chose', () => {
    const markup = render({
      identity_confident: true,
      identity_decision: 'number_unique',
      suggested_match_id: 'base1-59_en',
    })

    // Both candidates are rendered; exactly one carries the badge, and it is
    // the persisted id rather than the first-ranked card.
    expect(markup).toContain('data-candidate="base1-58_en"')
    expect(markup).toContain('data-candidate="base1-59_en"')
    expect(badgedCandidates(markup)).toEqual(['base1-59_en'])
  })

  it('badges one candidate when two of them share a tcg_card_id', () => {
    // The matcher picks a printing, not a card. Keying the badge on
    // tcg_card_id marks the German and the English row of the same card at
    // once, and the user is shown two "Suggested" candidates with no way to
    // tell which one was meant.
    const markup = render({
      matches: sameCardTwice,
      identity_confident: true,
      identity_decision: 'number_metadata',
      suggested_match_id: 'base1-4_de',
    })

    expect(markup).toContain('data-candidate="base1-4_de"')
    expect(markup).toContain('data-candidate="base1-4_en"')
    expect(badgedCandidates(markup)).toEqual(['base1-4_de'])
  })

  it('badges nothing when handed a bare card id instead of a candidate id', () => {
    // A card id cannot single out a candidate, so it is not a suggestion this
    // component can honour. Staying silent is the safe reading; falling back
    // to a tcg_card_id comparison would badge both rows below.
    const markup = render({
      matches: sameCardTwice,
      identity_confident: true,
      identity_decision: 'number_metadata',
      suggested_match_id: 'base1-4',
    })

    expect(markup).toContain('data-candidate="base1-4_de"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('leaves the badge out of the click target', () => {
    // The badge is painted at z-30 directly over the card's full-bleed select
    // button at z-25. Without pointer-events-none it is the hit target, and
    // clicking the thing labelled "Suggested" does nothing at all. There is no
    // DOM here to hit-test, so the class that disables hit-testing is the
    // contract this test pins; the behaviour itself was checked in a browser.
    const markup = render({
      identity_confident: true,
      suggested_match_id: 'base1-59_en',
    })

    const badged = candidateChunks(markup)
      .find(chunk => chunk.markup.includes('scanner.suggestedMatch'))
    expect(badged.id).toBe('base1-59_en')
    const badgeTag = badged.markup.slice(badged.markup.indexOf('<p'))
    expect(badgeTag.slice(0, badgeTag.indexOf('>'))).toContain('pointer-events-none')
  })

  it('renders the badge and the compare button together on the same candidate', () => {
    // Both overlay children live in one fragment. A fixture without an image
    // only ever exercises the badge on its own, so a regression that dropped
    // the compare button whenever a card was suggested would go unnoticed.
    const markup = render({
      matches: [
        { ...matches[0] },
        { ...matches[1], image: 'https://assets.tcgdex.net/base1-59/low.webp' },
      ],
      identity_confident: true,
      suggested_match_id: 'base1-59_en',
    })

    const chunks = candidateChunks(markup)
    const suggested = chunks.find(chunk => chunk.id === 'base1-59_en')
    expect(suggested.markup).toContain('scanner.suggestedMatch')
    expect(suggested.markup).toContain('scanner.compareCandidate')
    // The negative control on the same render: the candidate with no image
    // gets neither overlay child, so "contains" above is not just matching the
    // whole grid's markup.
    const plain = chunks.find(chunk => chunk.id === 'base1-58_en')
    expect(plain.markup).not.toContain('scanner.suggestedMatch')
    expect(plain.markup).not.toContain('scanner.compareCandidate')
  })

  it('leaves the first-ranked candidate unbadged when the matcher was not confident', () => {
    const markup = render({
      identity_confident: false,
      suggested_match_id: null,
    })

    expect(markup).toContain('data-candidate="base1-58_en"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('leaves the first-ranked candidate unbadged when no verdict was ever recorded', () => {
    // A scan queued before the verdict was persisted. Null is not false, and
    // neither one licenses a suggestion.
    const markup = render()

    expect(markup).toContain('data-candidate="base1-58_en"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('badges nothing when an id survives alongside a negative verdict', () => {
    // The queue clears the id whenever it clears confidence, so this pairing
    // should never arrive. The badge still refuses it: a suggestion the matcher
    // does not stand behind is exactly what must not be shown as one.
    const markup = render({
      identity_confident: false,
      suggested_match_id: 'base1-59_en',
    })

    expect(markup).toContain('data-candidate="base1-59_en"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('badges nothing when confidence is claimed without a suggested id', () => {
    const markup = render({ identity_confident: true, suggested_match_id: null })

    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('badges nothing when the suggested id is not among the candidates', () => {
    // The stored id must be matched exactly. Falling back to "first card" here
    // is precisely the dishonest badge this feature exists to remove.
    const markup = render({
      identity_confident: true,
      suggested_match_id: 'swsh1-1_en',
    })

    expect(markup).toContain('data-candidate="base1-58_en"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })
})
