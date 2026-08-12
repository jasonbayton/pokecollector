import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SCAN_JOBS_QUERY_KEY } from '../utils/scanJobs'
import UnifiedCardScanner from '../components/UnifiedCardScanner'
import CardSearch from './CardSearch'

const { useQuery, useMutation, openScanner, navigate, location, keysSuspended, scannerState } = vi.hoisted(() => ({
  useQuery: vi.fn(),
  useMutation: vi.fn(),
  openScanner: vi.fn(),
  navigate: vi.fn(),
  location: { pathname: '/search', search: '?q=charizard&rarity=Rare' },
  keysSuspended: vi.fn(() => false),
  scannerState: { openScanner: null, isScannerOpen: false, isCustomCardOpen: false },
}))

vi.mock('@tanstack/react-query', () => ({
  useQuery,
  useMutation,
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

vi.mock('react-router-dom', () => ({
  useLocation: () => location,
  useNavigate: () => navigate,
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key, settings: {}, formatPrice: value => `${value}` }),
}))

vi.mock('../contexts/ScannerContext', () => ({
  useScanner: () => ({ ...scannerState, openScanner }),
}))

// Stubbed so the arguments the page hands it can be read back. The rule itself
// is tested in utils/cardSearchOverlays.test.js; what matters here is which
// surfaces the page actually declares, because the guard runs in an effect and
// effects do not run in a server render.
vi.mock('../utils/cardSearchOverlays', () => ({ cardSearchKeysSuspended: keysSuspended }))

vi.mock('../api/client', () => ({
  searchCards: vi.fn(),
  getSets: vi.fn(),
  getCustomCards: vi.fn(),
  bulkAddToCollection: vi.fn(),
  enqueueScanJob: vi.fn(),
  getScanJobs: vi.fn(),
}))

vi.mock('../hooks/useVisibleTcgdexLanguages', () => ({
  useVisibleTcgdexLanguages: () => [{ code: 'en', label: 'English' }],
}))

vi.mock('../components/CardItem', () => ({
  CardItem: () => null,
  CustomCardModal: () => null,
  CardModal: () => null,
}))

vi.mock('../components/card-system', () => ({
  CardDisplay: () => null,
  CardLegend: () => null,
}))

vi.mock('../components/ui/Sheet', () => ({ default: () => null }))
vi.mock('../components/TcgdexLanguageSelect', () => ({ default: () => null }))
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

// Calling the page inside a render keeps its hooks valid while handing the test
// the element tree, and with it the handlers the header buttons were given.
const searchTree = () => {
  let captured
  function Capture() {
    captured = CardSearch()
    return null
  }
  renderToStaticMarkup(createElement(Capture))
  return [...walk(captured)]
}

beforeEach(() => {
  keysSuspended.mockClear()
  scannerState.isScannerOpen = false
  scannerState.isCustomCardOpen = false
  openScanner.mockReset()
  navigate.mockReset()
  useQuery.mockReset()
  useQuery.mockReturnValue({ data: undefined, isLoading: false, error: null, isFetching: false })
  useMutation.mockReset()
  useMutation.mockReturnValue({ mutate: vi.fn(), isPending: false })
})

describe('CardSearch scanner entry point', () => {
  it('opens the shared scanner instead of one of its own', () => {
    const scanButton = searchTree().find(node => node.props?.title === 'scanner.title')

    expect(scanButton).toBeDefined()
    scanButton.props.onClick()

    expect(openScanner).toHaveBeenCalledTimes(1)
    expect(navigate).not.toHaveBeenCalled()
  })

  it('no longer polls the scan queue for a badge of its own', () => {
    // The badge moved to the global control. A second query here would poll the
    // same endpoint every three seconds on top of it.
    searchTree()

    const scanQueries = useQuery.mock.calls
      .map(([options]) => options)
      .filter(options => options.queryKey === SCAN_JOBS_QUERY_KEY)

    expect(scanQueries).toHaveLength(0)
  })

  it('declares both panels the global control opens, not only its own modals', () => {
    // Its own modals the page knows about. The scanner and the manual card form
    // opened from the quick-add menu it can only learn about from the provider,
    // and while either owns the screen the left and right keys must not turn
    // the page behind it.
    scannerState.isScannerOpen = true
    scannerState.isCustomCardOpen = true

    searchTree()

    expect(keysSuspended).toHaveBeenCalled()
    expect(keysSuspended.mock.calls.at(-1)[0]).toMatchObject({
      scannerOpen: true,
      quickAddCustomCardOpen: true,
      cardDialogOpen: false,
      filtersOpen: false,
      pageCustomCardOpen: false,
    })
  })

  it('keeps its own scanner state out of the page', () => {
    // Nothing in the page may mount a scanner: the provider owns the only one.
    const types = searchTree().map(node => node.type)

    expect(types).not.toContain(UnifiedCardScanner)
  })
})
