import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ScanItemPanel } from './ScanReview'
import { CardDisplay } from './card-system'

// Picking a candidate is the whole point of the review inbox: it is what carries
// the scan item and the chosen match up to the queue, which opens the add dialog
// and resolves the scan from them. Losing that wiring is silent - the grid still
// renders, the cards still look clickable, and nothing happens. There is no DOM
// here to click with, so the JSX runtime is wrapped to hand the test the element
// tree the render produced, handlers included. A change of JSX transform would
// empty the recording, and every lookup below asserts on what it found.
const { rendered, fetchScanJobItemImage } = vi.hoisted(() => ({
  rendered: [],
  fetchScanJobItemImage: vi.fn(),
}))

const record = (type, props) => {
  rendered.push({ type, props })
}

vi.mock('react/jsx-dev-runtime', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    jsxDEV: (type, props, key, isStatic, source, self) => {
      record(type, props)
      return actual.jsxDEV(type, props, key, isStatic, source, self)
    },
  }
})

// Vitest transforms with the development runtime, but the production one is
// mocked too so a mode change cannot quietly stop the recording.
vi.mock('react/jsx-runtime', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    jsx: (type, props, key) => {
      record(type, props)
      return actual.jsx(type, props, key)
    },
    jsxs: (type, props, key) => {
      record(type, props)
      return actual.jsxs(type, props, key)
    },
  }
})

vi.mock('../api/client', () => ({ fetchScanJobItemImage }))

vi.mock('../contexts/SettingsContext', () => ({ useSettings: () => ({ t: key => key }) }))

const starmie = {
  id: 'base1-64_en',
  name: 'Starmie',
  image: '/api/images/card/base1-64_en/small',
  lang: 'en',
  number: '64',
  set_abbreviation: 'base1',
  tcg_card_id: 'base1-64',
}

const staryu = {
  id: 'base1-65_de',
  name: 'Staryu',
  image: '/api/images/card/base1-65_de/small',
  lang: 'de',
  number: '65',
  set_abbreviation: 'base1',
  tcg_card_id: 'base1-65',
}

const reviewItem = {
  id: 31,
  position: 0,
  status: 'done',
  has_image: false,
  recognized: { name: 'Starmie', language: 'de' },
  matches: [starmie, staryu],
}

const renderPanel = (props = {}) => {
  rendered.length = 0
  return renderToStaticMarkup(createElement(ScanItemPanel, {
    jobId: 7,
    item: reviewItem,
    onAdd: () => {},
    onRetry: () => {},
    onDismiss: () => {},
    retryNow: Date.now(),
    t: key => key,
    ...props,
  }))
}

const candidateCards = () => rendered.filter(entry => entry.type === CardDisplay)

beforeEach(() => {
  fetchScanJobItemImage.mockReset()
})

describe('picking a candidate from a scan item', () => {
  it('renders one selectable card per match', () => {
    renderPanel()

    const cards = candidateCards()
    expect(cards).toHaveLength(reviewItem.matches.length)
    expect(cards.map(entry => entry.props.card)).toEqual([starmie, staryu])
  })

  it('hands the scan item and the picked match to the queue when a card is selected', () => {
    // Both the card's own select affordance and a plain click on it mean the
    // same thing, and the queue needs the item as well as the match: it resolves
    // that scan with the card the user chose.
    const onAdd = vi.fn()
    renderPanel({ onAdd })
    const cards = candidateCards()

    cards[1].props.onSelect(staryu)

    expect(onAdd).toHaveBeenCalledTimes(1)
    expect(onAdd).toHaveBeenCalledWith(reviewItem, staryu)

    cards[0].props.onClick()

    expect(onAdd).toHaveBeenCalledTimes(2)
    expect(onAdd).toHaveBeenLastCalledWith(reviewItem, starmie)
  })

  it('does not add the card when the user only opens the comparison view', () => {
    // The control for the test above: the overlay sits on top of the very card
    // that adds, so a candidate must not be added by someone comparing it with
    // their photo first.
    const onAdd = vi.fn()
    renderPanel({ onAdd })

    const overlay = rendered.find(entry => (
      entry.type === 'button' && entry.props?.['aria-label'] === 'scanner.compareCandidate'
    ))
    expect(overlay, 'no comparison control was rendered over the candidate').toBeDefined()
    overlay.props.onClick({ stopPropagation: () => {} })

    expect(onAdd).not.toHaveBeenCalled()
  })
})
