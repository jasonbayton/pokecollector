import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ScanAddModal from './ScanAddModal'

// The modal portals into document.body, which the repository's node test
// environment does not have. Capturing the portal's tree instead of mounting it
// keeps the markup renderable and, more importantly, hands the test the real
// click handlers the component built during that render.
const { portalTrees, addToCollection, settings } = vi.hoisted(() => ({
  portalTrees: [],
  addToCollection: vi.fn(),
  settings: {},
}))

vi.mock('react-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  createPortal: (node) => {
    portalTrees.push(node)
    return node
  },
}))

vi.mock('../api/client', () => ({ addToCollection }))

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

vi.mock('react-hot-toast', () => ({
  default: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => settings,
}))

const match = {
  id: 'base1-64_en',
  name: 'Starmie',
  image: '/api/images/card/base1-64_en/small',
  lang: 'en',
  rarity: 'Rare',
  number: '64',
  set_abbreviation: 'base1',
  variants_normal: true,
}

function* walk(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child)
    return
  }
  if (!node || typeof node !== 'object') return
  yield node
  yield* walk(node.props?.children)
}

// The submit button is the only button the modal can disable; both close
// buttons are unconditional. Selecting on that rather than on a class string
// keeps the test pinned to behaviour instead of styling.
const findSubmitButton = tree => [...walk(tree)]
  .find(node => node.type === 'button' && typeof node.props?.disabled === 'boolean')

const render = (props = {}) => renderToStaticMarkup(
  createElement(ScanAddModal, { match, defaultLang: 'de', onClose: () => {}, ...props })
)

beforeEach(() => {
  portalTrees.length = 0
  addToCollection.mockReset()
  addToCollection.mockResolvedValue({})
  Object.assign(settings, {
    t: key => key,
    exchangeRate: 1,
    exchangeRateReady: true,
    currencySymbol: '€',
  })
  vi.stubGlobal('document', { body: {} })
})

describe('ScanAddModal', () => {
  it('identifies the card being added by name, set number and rarity', () => {
    const markup = render()

    expect(markup).toContain('Starmie')
    expect(markup).toContain('BASE1 64')
    expect(markup).toContain('Rare')
  })

  it('offers Mint as the preselected condition', () => {
    const markup = render()

    expect(markup).toContain('<option value="Mint" selected="">Mint</option>')
    expect(markup).not.toContain('<option value="NM" selected="">NM</option>')
  })

  it('adds the scanned card on its own defaults without the user touching a control', async () => {
    render()
    const submit = findSubmitButton(portalTrees[0])
    expect(submit).toBeDefined()

    await submit.props.onClick()

    expect(addToCollection).toHaveBeenCalledTimes(1)
    expect(addToCollection).toHaveBeenCalledWith({
      card_id: 'base1-64_en',
      quantity: 1,
      condition: 'Mint',
      variant: 'Normal',
      lang: 'en',
      purchase_price: undefined,
    })
  })

  it('prefers the review item language when the match itself has none', async () => {
    render({ match: { ...match, lang: null } })

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(addToCollection).toHaveBeenCalledWith(expect.objectContaining({ lang: 'de' }))
  })

  it('refuses to add while the exchange rate is unknown, so no price is stored at the wrong rate', async () => {
    settings.exchangeRateReady = false
    render()
    const submit = findSubmitButton(portalTrees[0])

    // The disabled attribute is the visible half of the gate; handleAdd
    // enforces it a second time so a programmatic click cannot slip past.
    expect(submit.props.disabled).toBe(true)
    await submit.props.onClick()
    expect(addToCollection).not.toHaveBeenCalled()
  })
})
