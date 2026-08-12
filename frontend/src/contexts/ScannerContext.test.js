import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SCAN_JOBS_QUERY_KEY } from '../utils/scanJobs'
import UnifiedCardScanner from '../components/UnifiedCardScanner'
import {
  QUICK_ADD_CUSTOM,
  QUICK_ADD_QUEUE,
  QUICK_ADD_SCAN,
  QUICK_ADD_SEARCH,
  ScannerProvider,
  quickAddDestination,
  useScanner,
} from './ScannerContext'

const { useQuery, invalidateQueries, navigate, apiStubs } = vi.hoisted(() => ({
  useQuery: vi.fn(),
  invalidateQueries: vi.fn(),
  navigate: vi.fn(),
  apiStubs: { getScanJobs: vi.fn(), enqueueScanJob: vi.fn() },
}))

vi.mock('@tanstack/react-query', () => ({
  useQuery,
  useQueryClient: () => ({ invalidateQueries }),
}))

vi.mock('../api/client', () => apiStubs)

// useLocation stays real: the provider reads the live pathname to decide whether
// a quick-add action needs to move the user at all.
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  useNavigate: () => navigate,
}))

vi.mock('./SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

let scanner

function Probe() {
  scanner = useScanner()
  return createElement('p', null, 'page content')
}

const render = ({ entries = ['/collection'], child = createElement(Probe) } = {}) =>
  renderToStaticMarkup(createElement(
    MemoryRouter,
    { initialEntries: entries },
    createElement(ScannerProvider, null, child),
  ))

beforeEach(() => {
  scanner = null
  navigate.mockReset()
  invalidateQueries.mockReset()
  useQuery.mockReset()
  useQuery.mockReturnValue({ data: { jobs: [] } })
})

describe('quickAddDestination', () => {
  it('sends the catalogue and queue actions to their pages', () => {
    expect(quickAddDestination(QUICK_ADD_SEARCH, '/collection')).toBe('/search')
    expect(quickAddDestination(QUICK_ADD_QUEUE, '/collection')).toBe('/scans')
  })

  it('refuses to re-enter the page the user is already on', () => {
    // Navigating to /search from /search replaces the search string, which is
    // where the card search keeps its query, its filters and its page number.
    expect(quickAddDestination(QUICK_ADD_SEARCH, '/search')).toBeNull()
    expect(quickAddDestination(QUICK_ADD_QUEUE, '/scans')).toBeNull()
  })

  it('keeps the dialog actions on the current page', () => {
    expect(quickAddDestination(QUICK_ADD_SCAN, '/collection')).toBeNull()
    expect(quickAddDestination(QUICK_ADD_SCAN, '/search')).toBeNull()
    expect(quickAddDestination(QUICK_ADD_CUSTOM, '/sets')).toBeNull()
  })
})

describe('ScannerProvider', () => {
  it('opens the scanner without leaving the page the user is on', () => {
    const markup = render({ entries: ['/collection'] })

    scanner.openScanner()
    scanner.closeScanner()

    // No navigation at all: the page below keeps its component instances, and
    // with them its own state.
    expect(navigate).not.toHaveBeenCalled()
    expect(markup).toContain('<p>page content</p>')
  })

  it('opens the scanner from the card search without touching its URL state', () => {
    render({ entries: ['/search?q=charizard&rarity=Rare&page=3'] })

    scanner.runQuickAdd(QUICK_ADD_SCAN)

    expect(navigate).not.toHaveBeenCalled()
  })

  it('still walks to the catalogue and the queue from elsewhere', () => {
    render({ entries: ['/collection'] })

    scanner.runQuickAdd(QUICK_ADD_SEARCH)
    scanner.runQuickAdd(QUICK_ADD_QUEUE)

    expect(navigate).toHaveBeenNthCalledWith(1, '/search')
    expect(navigate).toHaveBeenNthCalledWith(2, '/scans')
  })

  it('runs one scan-queue query for the whole app, polling only while work is in flight', () => {
    render()

    const scanQueries = useQuery.mock.calls
      .map(([options]) => options)
      .filter(options => options.queryKey === SCAN_JOBS_QUERY_KEY)

    expect(scanQueries).toHaveLength(1)
    expect(scanQueries[0].queryFn).toBe(apiStubs.getScanJobs)
    expect(scanQueries[0].refetchInterval({ state: { data: { jobs: [{ active: 1 }] } } })).toBe(3000)
    expect(scanQueries[0].refetchInterval({ state: { data: { jobs: [{ active: 0 }] } } })).toBe(false)
  })

  it('publishes the queue badge numbers to whatever renders the control', () => {
    useQuery.mockReturnValue({ data: { jobs: [{ attention: 3, active: 0 }, { attention: 2, active: 4 }] } })

    render()

    expect(scanner.scanAttention).toBe(5)
    expect(scanner.scansActive).toBe(true)
  })

  it('reports nothing to show once every item is resolved and no job is running', () => {
    useQuery.mockReturnValue({ data: { jobs: [{ attention: 0, active: 0 }, { attention: 0, active: 0 }] } })

    render()

    expect(scanner.scanAttention).toBe(0)
    expect(scanner.scansActive).toBe(false)
  })

  it('mounts the real scanner, closed, alongside the page rather than in place of it', () => {
    // The scanner has to be a sibling of the page and stay mounted: it bumps a
    // generation counter on open and close so a batch that finishes uploading
    // after the user walked away cannot drag them to the new job.
    const tree = scannerProviderTree()

    const scannerElement = tree.find(node => node.type === UnifiedCardScanner)
    expect(scannerElement).toBeDefined()
    expect(scannerElement.props.isOpen).toBe(false)
    expect(typeof scannerElement.props.onClose).toBe('function')
    expect(tree.some(node => node.type === 'p')).toBe(true)
  })

  it('cannot be mounted outside the router it navigates with', () => {
    // Quick add moves the user to /search and /scans, and the scanner sends
    // them to the new job. Mounted beside ConfirmDialogProvider, above
    // BrowserRouter, none of that can work.
    expect(() => renderToStaticMarkup(
      createElement(ScannerProvider, null, createElement('p', null, 'page content')),
    )).toThrow(/Router/)
  })
})

// Renders the provider once more and returns its element tree, flattened.
function scannerProviderTree() {
  let captured
  function Capture() {
    captured = ScannerProvider({ children: createElement('p', null, 'page content') })
    return null
  }
  renderToStaticMarkup(createElement(
    MemoryRouter,
    { initialEntries: ['/collection'] },
    createElement(Capture),
  ))
  return [...walk(captured)]
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
