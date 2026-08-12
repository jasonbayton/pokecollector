import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ScanQueue, { hasInAppPredecessor } from './ScanQueue'
import ScanAddModal from '../components/ScanAddModal'
import AppNav from '../components/AppNav'

// The queue is a page now, so what has to be proven is which controls it renders
// and where each one navigates. The repository's test environment has no DOM, so
// there is nothing to click: the JSX runtime is wrapped instead, which hands the
// test the very element tree the render produced, handlers included. A change of
// JSX transform would empty the recording, and every lookup below asserts the
// control was found, so that failure is loud rather than silent.
const {
  rendered, portalTrees, portalTargets, navigate, route, settings, auth,
  queryOptions, queryResults, mutationOptions, api, toastMock,
  invalidateCardState, invalidateTcgdexFilterLanguages, parseMoneyInputValue,
} = vi.hoisted(() => ({
  rendered: [],
  portalTrees: [],
  portalTargets: [],
  navigate: vi.fn(),
  route: { params: {}, location: { pathname: '/scans', state: null } },
  settings: {},
  auth: {},
  queryOptions: [],
  queryResults: new Map(),
  mutationOptions: [],
  api: {
    getScanJobs: vi.fn(),
    getScanJob: vi.fn(),
    deleteScanJob: vi.fn(),
    resolveScanJobItem: vi.fn(),
    retryScanJobItem: vi.fn(),
    fetchScanJobItemImage: vi.fn(),
    addToCollection: vi.fn(),
  },
  toastMock: { success: vi.fn(), error: vi.fn() },
  invalidateCardState: vi.fn(),
  invalidateTcgdexFilterLanguages: vi.fn(),
  parseMoneyInputValue: vi.fn(),
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

vi.mock('react-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  createPortal: (node, container) => {
    portalTrees.push(node)
    portalTargets.push(container)
    return node
  },
}))

vi.mock('react-router-dom', () => ({
  useNavigate: () => navigate,
  useParams: () => route.params,
  useLocation: () => route.location,
}))

vi.mock('@tanstack/react-query', () => ({
  useQuery: (options) => {
    queryOptions.push(options)
    return queryResults.get(String(options.queryKey)) || { data: undefined, isLoading: false, isError: false }
  },
  useMutation: (options) => {
    mutationOptions.push(options)
    return { mutate: vi.fn(), isPending: false }
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

vi.mock('../api/client', () => api)

vi.mock('../contexts/SettingsContext', () => ({ useSettings: () => settings }))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => auth }))

vi.mock('react-hot-toast', () => ({ default: toastMock }))

vi.mock('../utils/queryInvalidation', () => ({
  invalidateCardState,
  invalidateTcgdexFilterLanguages,
}))

vi.mock('../utils/moneyInput', () => ({ parseMoneyInputValue }))

const activeJob = {
  id: 7,
  processed: 1,
  total: 3,
  attention: 1,
  failed_attention: 0,
  active: 2,
  retrying: 0,
  pending: 2,
  processing: 0,
  failed: 0,
  expires_at: '2026-09-01T00:00:00.000Z',
}

const jobDetail = {
  ...activeJob,
  items: [{
    id: 31,
    position: 0,
    status: 'done',
    matches: [],
    has_image: true,
    recognized: { name: 'Starmie', language: 'de' },
  }],
}

const candidate = {
  id: 'base1-64_en',
  name: 'Starmie',
  image: '/api/images/card/base1-64_en/small',
  lang: 'en',
  number: '64',
  set_abbreviation: 'base1',
  variants_normal: true,
}

// Text rather than class names or element order: the labels are the part a user
// actually sees, and they survive restyling.
const textOf = (node) => {
  if (node === null || node === undefined || typeof node === 'boolean') return ''
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (typeof node === 'object') return textOf(node.props?.children)
  return String(node)
}

const buttonLabelled = (label) => rendered.find(entry => (
  entry.type === 'button' && textOf(entry.props?.children).includes(label)
))

const clickButton = (label) => {
  const button = buttonLabelled(label)
  expect(button, `no button labelled ${label} was rendered`).toBeDefined()
  button.props.onClick({ stopPropagation: () => {} })
}

const renderScanQueue = () => {
  rendered.length = 0
  portalTrees.length = 0
  portalTargets.length = 0
  queryOptions.length = 0
  mutationOptions.length = 0
  return renderToStaticMarkup(createElement(ScanQueue))
}

const optionsForKey = (key) => queryOptions.find(options => String(options.queryKey) === key)

const showList = (jobs = [activeJob]) => {
  route.params = {}
  route.location = { pathname: '/scans', state: null }
  queryResults.set('scan-jobs', { data: { jobs }, isLoading: false, isError: false })
}

const showDetail = ({ fromScanQueue = false } = {}) => {
  route.params = { jobId: '7' }
  route.location = { pathname: '/scans/7', state: fromScanQueue ? { fromScanQueue: true } : null }
  queryResults.set('scan-job,7', { data: jobDetail, isLoading: false, isError: false })
}

let documentStub

beforeEach(() => {
  navigate.mockReset()
  queryResults.clear()
  Object.assign(settings, {
    t: key => key,
    exchangeRate: 1,
    exchangeRateReady: true,
    currencySymbol: '€',
  })
  Object.assign(auth, { user: null, logout: () => {}, multiUser: false })
  parseMoneyInputValue.mockReset()
  documentStub = { body: {} }
  vi.stubGlobal('document', documentStub)
  vi.stubGlobal('window', { history: { state: { idx: 0 } } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the scan queue as a page', () => {
  it('renders the queue inline, with no dialog wrapped around it', () => {
    showList()

    const markup = renderScanQueue()

    // Not vacuous: the list content is on the page, it simply is not in a dialog.
    expect(markup).toContain('scanner.queueSubtitle')
    expect(markup).toContain('scanner.processed')
    expect(portalTargets).toHaveLength(0)
    expect(markup).not.toContain('aria-modal')
    expect(markup).not.toContain('role="dialog"')
  })

  it('leaves the job detail inline too', () => {
    showDetail()

    const markup = renderScanQueue()

    expect(markup).toContain('scanner.backToScans')
    expect(portalTargets).toHaveLength(0)
    expect(markup).not.toContain('aria-modal')
    expect(markup).not.toContain('role="dialog"')
  })

  it('still puts a chosen candidate into a dialog layer of its own', () => {
    // The control for the two assertions above. They only mean something if this
    // harness can see a dialog at all, and this is the dialog the review inbox
    // opens when a candidate is picked, portalled clear of the page.
    portalTargets.length = 0
    const markup = renderToStaticMarkup(createElement(ScanAddModal, {
      match: candidate,
      defaultLang: 'de',
      onClose: () => {},
    }))

    expect(portalTargets).toEqual([documentStub.body])
    expect(markup).toContain('fixed inset-0')
    expect(markup).toContain('scanner.addToCollection')
  })
})

describe('list to detail navigation', () => {
  it('opens a job from its row, marked as arriving from the list', () => {
    showList()
    renderScanQueue()

    clickButton('scanner.processed')

    expect(navigate).toHaveBeenCalledWith('/scans/7', { state: { fromScanQueue: true } })
  })

  it('sends the detail back button to the queue, never to the search fallback', () => {
    // The control for the row test: forward navigation must not have been bought
    // by breaking the way back. A detail reached without the marker (the scanner
    // pushes straight to a fresh job) replaces itself, so the history index the
    // list later reads is the one the user actually arrived on.
    showDetail()
    renderScanQueue()

    clickButton('scanner.backToScans')

    expect(navigate).toHaveBeenCalledWith('/scans', { replace: true })
    expect(navigate).not.toHaveBeenCalledWith('/search')
  })

  it('pops back to the list entry a row came from instead of stacking another', () => {
    showDetail({ fromScanQueue: true })
    renderScanQueue()

    clickButton('scanner.backToScans')

    expect(navigate).toHaveBeenCalledWith(-1)
  })
})

describe('leaving the queue', () => {
  it('falls back to the search page when the queue was loaded directly', () => {
    vi.stubGlobal('window', { history: { state: { idx: 0 } } })
    showList()
    renderScanQueue()

    clickButton('common.back')

    expect(navigate).toHaveBeenCalledWith('/search')
  })

  it('goes back through history when an in-app page came first', () => {
    // The control for the fallback: reaching the queue from, say, the collection
    // must return there rather than dumping the user on the search page. Only the
    // pop can be observed here, since this environment has no history to pop.
    vi.stubGlobal('window', { history: { state: { idx: 3 } } })
    showList()
    renderScanQueue()

    clickButton('common.back')

    expect(navigate).toHaveBeenCalledWith(-1)
    expect(navigate).not.toHaveBeenCalledWith('/search')
  })

  it('reads a predecessor only from a history index above the entry the tab opened on', () => {
    expect(hasInAppPredecessor({ idx: 1 })).toBe(true)
    expect(hasInAppPredecessor({ idx: 0 })).toBe(false)
    expect(hasInAppPredecessor({ idx: null })).toBe(false)
    expect(hasInAppPredecessor(null)).toBe(false)
    expect(hasInAppPredecessor(undefined)).toBe(false)
  })

  it('keeps the empty state pointing at the page that hosts the scanner', () => {
    // History would drop the user wherever they came from, which need not have a
    // way to scan. It is still one click short of an open scanner. The index is
    // set past the first entry so that wiring this button to the leave handler
    // instead, as it used to be, fails here rather than agreeing by accident.
    vi.stubGlobal('window', { history: { state: { idx: 3 } } })
    showList([])
    renderScanQueue()

    clickButton('scanner.goScan')

    expect(navigate).toHaveBeenCalledWith('/search')
  })
})

describe('the scan routes in the app title bar', () => {
  const renderNav = (pathname) => {
    route.location = { pathname, state: null }
    return renderToStaticMarkup(createElement(AppNav))
  }

  it('titles both the queue and a job detail with the queue title', () => {
    expect(renderNav('/scans')).toContain('scanner.queueTitle')
    expect(renderNav('/scans/7')).toContain('scanner.queueTitle')
  })

  it('still titles the search page as the card search', () => {
    const markup = renderNav('/search')

    expect(markup).toContain('nav.cardSearch')
    expect(markup).not.toContain('scanner.queueTitle')
  })
})

describe('what the page has to keep doing', () => {
  it('keeps polling the list while any job is working', () => {
    showList()
    renderScanQueue()
    const { refetchInterval } = optionsForKey('scan-jobs')

    expect(refetchInterval({ state: { data: { jobs: [activeJob] } } })).toBe(3000)
    expect(refetchInterval({ state: { data: { jobs: [{ ...activeJob, active: 0 }] } } })).toBe(false)
  })

  it('keeps polling the open job while it is working', () => {
    showDetail()
    renderScanQueue()
    const { refetchInterval } = optionsForKey('scan-job,7')

    expect(refetchInterval({ state: { data: jobDetail } })).toBe(3000)
    expect(refetchInterval({ state: { data: { ...jobDetail, active: 0 } } })).toBe(false)
  })

  it('shows a waiting row its retry countdown rather than the processing label', () => {
    showList([{
      ...activeJob,
      active: 1,
      retrying: 1,
      next_retry_at: new Date(Date.now() + 30_000).toISOString(),
    }])

    const markup = renderScanQueue()

    expect(markup).toContain('scanner.retryingInSeconds')
    expect(markup).not.toContain('scanner.processing')
  })
})
