import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ScanAddModal from './ScanAddModal'

// The modal portals into document.body, which the repository's node test
// environment does not have. Capturing the portal's tree instead of mounting it
// keeps the markup renderable and, more importantly, hands the test the real
// click handlers the component built during that render.
const {
  portalTrees, portalContainers, addToCollection, settings,
  invalidateCardState, invalidateTcgdexFilterLanguages, parseMoneyInputValue, toastMock,
} = vi.hoisted(() => ({
  portalTrees: [],
  portalContainers: [],
  addToCollection: vi.fn(),
  settings: {},
  invalidateCardState: vi.fn(),
  invalidateTcgdexFilterLanguages: vi.fn(),
  parseMoneyInputValue: vi.fn(),
  toastMock: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('react-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  // The container is captured as well as the tree: createPortal(node, null)
  // throws in a browser, and a test that ignores the second argument cannot
  // tell that the target was lost while moving the component between modules.
  createPortal: (node, container) => {
    portalTrees.push(node)
    portalContainers.push(container)
    return node
  },
}))

vi.mock('../utils/queryInvalidation', () => ({
  invalidateCardState,
  invalidateTcgdexFilterLanguages,
}))

vi.mock('../utils/moneyInput', () => ({ parseMoneyInputValue }))

vi.mock('../api/client', () => ({ addToCollection }))

vi.mock('@tanstack/react-query', () => ({
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

vi.mock('react-hot-toast', () => ({ default: toastMock }))

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
// Identified by its label rather than by "the first button carrying a boolean
// disabled prop". That older rule quietly selected whichever button happened to
// be disable-able first, so giving the close control a disabled state made it
// match instead and three tests started exercising the wrong button.
const textOfNode = node => [...walk(node)]
  .flatMap(child => (Array.isArray(child.props?.children) ? child.props.children : [child.props?.children]))
  .filter(child => typeof child === 'string')
  .join(' ')

const findSubmitButton = tree => [...walk(tree)].find(node => (
  node.type === 'button'
  && /scanner\.(addToCollection|adding)/.test(textOfNode(node))
))

let documentStub

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
  invalidateCardState.mockReset()
  invalidateTcgdexFilterLanguages.mockReset()
  parseMoneyInputValue.mockReset()
  parseMoneyInputValue.mockReturnValue(undefined)
  toastMock.success.mockReset()
  toastMock.error.mockReset()
  portalContainers.length = 0
  documentStub = { body: {} }
  vi.stubGlobal('document', documentStub)
})

// Stated rather than inherited: without this the stub survives the file, and a
// future non-isolated run would hand other node tests a document with a body.
afterEach(() => {
  vi.unstubAllGlobals()
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
  it('hands the scan queue its completion callback, then closes', async () => {
    // ScanQueue resolves the review item from onAdded. Losing it adds the card
    // to the collection while the scan stays unresolved forever, so the user
    // re-adds it and duplicates. onAdded must fire before onClose, or the
    // parent unmounts the modal before its own callback runs.
    const calls = []
    render({ onAdded: () => calls.push('onAdded'), onClose: () => calls.push('onClose') })

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(calls).toEqual(['onAdded', 'onClose'])
  })

  it('refreshes the caches the new card invalidates', async () => {
    render()

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(invalidateCardState).toHaveBeenCalledTimes(1)
    expect(invalidateTcgdexFilterLanguages).toHaveBeenCalledTimes(1)
  })

  it('portals into the document body', () => {
    // createPortal(node, null) throws "Target container is not a DOM element".
    render()

    expect(portalContainers).toHaveLength(1)
    expect(portalContainers[0]).toBe(documentStub.body)
  })

  it('stores the typed price through the money parser, converted at the current rate', async () => {
    // The payload must carry the parser's result. Hardcoding undefined passes a
    // test that only ever leaves the price box empty.
    parseMoneyInputValue.mockReturnValue(12.34)
    settings.exchangeRate = 0.86
    render()

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(parseMoneyInputValue).toHaveBeenCalledWith('', 0.86)
    expect(addToCollection).toHaveBeenCalledWith(expect.objectContaining({ purchase_price: 12.34 }))
  })

  it('defaults to a print the card actually has when there is no Normal', async () => {
    // Both branches of getDefaultVariant return Normal for a normal-capable
    // card, so only a holo-only fixture can tell the helper from a constant.
    render({ match: { ...match, variants_normal: false, variants_holo: true } })

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(addToCollection).toHaveBeenCalledWith(expect.objectContaining({ variant: 'Holo' }))
  })

  it('every dismiss control closes the modal', () => {
    const onClose = vi.fn()
    render({ onClose })

    const closers = [...walk(portalTrees[0])]
      .filter(node => node.props?.onClick === onClose)

    // Backdrop, corner X and footer X.
    expect(closers).toHaveLength(3)
  })

  it('surfaces the API reason when the add fails, and does not close', async () => {
    addToCollection.mockRejectedValue({ response: { data: { detail: 'Quantity limit reached' } } })
    const onClose = vi.fn()
    const onAdded = vi.fn()
    render({ onClose, onAdded })

    await findSubmitButton(portalTrees[0]).props.onClick()

    expect(toastMock.error).toHaveBeenCalledWith('Quantity limit reached')
    expect(onAdded).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    expect(invalidateCardState).not.toHaveBeenCalled()
  })
})

describe('closing while an add is in flight', () => {
  it('binds the close control to the same in-flight state that guards submit', () => {
    // addToCollection writes to the collection BEFORE the scan is resolved and
    // unmounting does not cancel it, so closing mid-flight let the write land
    // after a re-take had already reset the item: a card nothing had matched,
    // filed, with the follow-up resolve failing 409 and no rollback.
    //
    // This suite renders to static markup, so component state never survives a
    // render and the in-flight case cannot be observed directly. What can be
    // checked is that the two controls are driven by the same flag: submit and
    // close must both carry a boolean disabled prop, so the close control
    // cannot silently go back to being always-enabled.
    render()
    const tree = portalTrees[portalTrees.length - 1]

    const submit = findSubmitButton(tree)
    const closeButton = [...walk(tree)]
      .find(node => node.type === 'button' && node.props?.['aria-label'] === 'common.close')

    expect(typeof submit.props.disabled).toBe('boolean')
    expect(closeButton, 'no close control was rendered').toBeDefined()
    expect(typeof closeButton.props.disabled).toBe('boolean')
  })
})
