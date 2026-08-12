import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ScanItemPanel } from './ScanReview'

// CardDisplay is stubbed down to the one thing this test needs to read: which
// candidate a piece of markup belongs to. It still renders the overlay it is
// handed, because that is where the badge lives - a stub that dropped the
// overlay would report every card as unbadged and pass on a broken component.
vi.mock('./card-system', () => ({
  CardDisplay: ({ card, overlay }) => createElement(
    'div',
    { 'data-candidate': card.tcg_card_id },
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
function badgedCandidates(markup) {
  return markup.split('data-candidate="').slice(1)
    .filter(chunk => chunk.includes('scanner.suggestedMatch'))
    .map(chunk => chunk.slice(0, chunk.indexOf('"')))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ScanItemPanel suggested match', () => {
  it('badges the candidate the matcher actually chose', () => {
    const markup = render({
      identity_confident: true,
      identity_decision: 'number_unique',
      suggested_match_id: 'base1-59',
    })

    // Both candidates are rendered; exactly one carries the badge, and it is
    // the persisted id rather than the first-ranked card.
    expect(markup).toContain('data-candidate="base1-58"')
    expect(markup).toContain('data-candidate="base1-59"')
    expect(badgedCandidates(markup)).toEqual(['base1-59'])
  })

  it('leaves the first-ranked candidate unbadged when the matcher was not confident', () => {
    const markup = render({
      identity_confident: false,
      suggested_match_id: null,
    })

    expect(markup).toContain('data-candidate="base1-58"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('leaves the first-ranked candidate unbadged when no verdict was ever recorded', () => {
    // A scan queued before the verdict was persisted. Null is not false, and
    // neither one licenses a suggestion.
    const markup = render()

    expect(markup).toContain('data-candidate="base1-58"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })

  it('badges nothing when an id survives alongside a negative verdict', () => {
    // The queue clears the id whenever it clears confidence, so this pairing
    // should never arrive. The badge still refuses it: a suggestion the matcher
    // does not stand behind is exactly what must not be shown as one.
    const markup = render({
      identity_confident: false,
      suggested_match_id: 'base1-59',
    })

    expect(markup).toContain('data-candidate="base1-59"')
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
      suggested_match_id: 'swsh1-1',
    })

    expect(markup).toContain('data-candidate="base1-58"')
    expect(markup).not.toContain('scanner.suggestedMatch')
  })
})
