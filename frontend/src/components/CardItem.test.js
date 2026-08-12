import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CustomCardModal } from './CardItem'

const {
  mutationOptions, portalTrees, state, addToCollection, queryClient,
} = vi.hoisted(() => ({
  mutationOptions: [],
  portalTrees: [],
  state: { calls: 0 },
  addToCollection: vi.fn(),
  queryClient: { invalidateQueries: vi.fn() },
}))

// Server rendering does not apply state changes, so give the first state slot -
// the card name - a value while retaining React's real hook bookkeeping. That
// lets this test exercise the form's genuine submit handler without jsdom.
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useState: initial => {
      const [value, setValue] = actual.useState(initial)
      state.calls += 1
      return [state.calls === 1 ? 'Abandon Card' : value, setValue]
    },
  }
})

vi.mock('react-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  createPortal: node => {
    portalTrees.push(node)
    return node
  },
}))

vi.mock('@tanstack/react-query', () => ({
  useQuery: () => ({ data: [] }),
  useQueryClient: () => queryClient,
  useMutation: options => {
    mutationOptions.push(options)
    return {
      isPending: false,
      mutate: data => options.onSuccess?.({ data: { id: 'manual-1', name: 'Abandon Card', lang: 'en' } }, data),
    }
  },
}))

vi.mock('../api/client', () => ({
  addToCollection,
  addToWishlist: vi.fn(),
  cloneCustomCard: vi.fn(),
  createCustomCard: vi.fn(),
  updateCustomCard: vi.fn(),
  updateCardCustomImage: vi.fn(),
  deleteCustomCard: vi.fn(),
  getSets: vi.fn(),
  getPriceHistory: vi.fn(),
  updateCollectionItem: vi.fn(),
  removeFromCollection: vi.fn(),
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: null }) }))
vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key, settings: { language: 'en' }, exchangeRate: 1, exchangeRateReady: true }),
}))
vi.mock('../contexts/ConfirmDialogContext', () => ({ useConfirmDialog: () => vi.fn() }))
vi.mock('../utils/queryInvalidation', () => ({
  invalidateCardState: vi.fn(),
  invalidateTcgdexFilterLanguages: vi.fn(),
}))
vi.mock('../utils/moneyInput', () => ({ parseMoneyInputValue: vi.fn() }))
vi.mock('./MoneyInput', () => ({ default: () => null }))
vi.mock('./TcgdexLanguageSelect', () => ({ default: () => null }))
vi.mock('./UnifiedCard', () => ({ default: () => null, UnifiedCardDialog: () => null }))
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn(), error: vi.fn() } }))

function* walk(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child)
    return
  }
  if (!node || typeof node !== 'object') return
  yield node
  yield* walk(node.props?.children)
}

beforeEach(() => {
  mutationOptions.length = 0
  portalTrees.length = 0
  state.calls = 0
  addToCollection.mockReset()
  queryClient.invalidateQueries.mockReset()
  vi.stubGlobal('document', { body: {} })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('CustomCardModal', () => {
  it('notifies the global owner after creation even when its add-to-collection step is abandoned', async () => {
    // The bystander is addToCollection: dismissing the second step must not add
    // a card, but search must still be told about the successful card creation.
    const events = []
    const onCreated = vi.fn(() => events.push('created'))
    const onClose = vi.fn(() => events.push('closed'))

    renderToStaticMarkup(createElement(CustomCardModal, {
      autoAddCollection: true,
      onCreated,
      onClose,
    }))

    const form = [...walk(portalTrees[0])].find(node => node.type === 'form')
    expect(form).toBeDefined()
    await form.props.onSubmit({ preventDefault: vi.fn() })

    // This is the create-then-dismiss exit path: creation succeeded, but the
    // user closes the optional collection step instead of adding the card.
    const closeControl = [...walk(portalTrees[0])]
      .find(node => node.type === 'button' && node.props?.onClick === onClose)
    expect(closeControl).toBeDefined()
    closeControl.props.onClick()

    expect(events).toEqual(['created', 'closed'])
    expect(onCreated).toHaveBeenCalledWith({ id: 'manual-1', name: 'Abandon Card', lang: 'en' })
    expect(addToCollection).not.toHaveBeenCalled()
  })
})
