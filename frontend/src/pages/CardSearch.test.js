import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SCAN_JOBS_QUERY_KEY } from '../utils/scanJobs'
import UnifiedCardScanner from '../components/UnifiedCardScanner'
import CardSearch from './CardSearch'

const { useQuery, useMutation, openScanner, navigate, location } = vi.hoisted(() => ({
  useQuery: vi.fn(),
  useMutation: vi.fn(),
  openScanner: vi.fn(),
  navigate: vi.fn(),
  location: { pathname: '/search', search: '?q=charizard&rarity=Rare' },
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
  useScanner: () => ({ openScanner, isScannerOpen: false }),
}))

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

  it('keeps its own scanner state out of the page', () => {
    // Nothing in the page may mount a scanner: the provider owns the only one.
    const types = searchTree().map(node => node.type)

    expect(types).not.toContain(UnifiedCardScanner)
  })
})
