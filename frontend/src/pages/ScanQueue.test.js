import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ScanQueue, { hasInAppPredecessor } from './ScanQueue'
import ScanAddModal from '../components/ScanAddModal'
import { ScanItemPanel } from '../components/ScanReview'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import AppNav from '../components/AppNav'

// The queue is a page now, so what has to be proven is which controls it renders
// and where each one navigates. The repository's test environment has no DOM, so
// there is nothing to click: the JSX runtime is wrapped instead, which hands the
// test the very element tree the render produced, handlers included. A change of
// JSX transform would empty the recording, and every lookup below asserts the
// control was found, so that failure is loud rather than silent.
const {
  rendered, portalTrees, portalTargets, navigate, route, settings, auth,
  queryOptions, queryResults, mutations, api, toastMock,
  invalidateCardState, invalidateTcgdexFilterLanguages, parseMoneyInputValue,
  stateSeeds, seedsConsumed, setAddSelection, setConfirmation,
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
  mutations: [],
  stateSeeds: [],
  seedsConsumed: [],
  setAddSelection: vi.fn(),
  setConfirmation: vi.fn(),
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
  // The mutate spy is kept next to the options that built it, so a test can
  // both fire a mutation and run the callbacks the component gave it.
  useMutation: (options) => {
    const mutate = vi.fn()
    mutations.push({ ...options, mutate })
    return { mutate, isPending: false }
  },
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
}))

// Some of what this page does is only reachable once a piece of its state is
// set: the add dialog mounts on a chosen candidate, and the destructive
// confirmation opens on a pending action. Nothing can click here, so the state
// is seeded instead. JobDetail is the only component in this tree that
// initialises state to null before its children render, and it does so twice -
// the chosen candidate first, then the pending destructive action - so seeds are
// handed only to null initialisers, in that order. The retry clock, the item
// photo and the zoom state keep their real implementations, and a seeded test
// asserts which seeds were taken, so a reordering fails loudly instead of
// quietly moving a seed onto different state.
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useState: (initial) => {
      const real = actual.useState(initial)
      if (initial !== null || stateSeeds.length === 0) return real
      const seed = stateSeeds.shift()
      seedsConsumed.push(seed.name)
      return [seed.value, seed.setter]
    },
  }
})

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

// A second unresolved item: with one item every "the last item was resolved"
// path is also the "some item was resolved" path, and a page that rendered only
// its first item would look complete.
const twoItemJob = {
  ...jobDetail,
  items: [
    jobDetail.items[0],
    { id: 32, position: 1, status: 'done', matches: [], has_image: true, recognized: { name: 'Staryu', language: 'de' } },
  ],
}

const candidate = {
  id: 'base1-64_en',
  name: 'Starmie',
  image: '/api/images/card/base1-64_en/small',
  lang: 'en',
  number: '64',
  set_abbreviation: 'base1',
  tcg_card_id: 'base1-64',
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

const elementsOf = (type) => rendered.filter(entry => entry.type === type)

const onlyElementOf = (type, what) => {
  const found = elementsOf(type)
  expect(found, `expected exactly one ${what}`).toHaveLength(1)
  return found[0]
}

const renderScanQueue = () => {
  rendered.length = 0
  portalTrees.length = 0
  portalTargets.length = 0
  queryOptions.length = 0
  mutations.length = 0
  return renderToStaticMarkup(createElement(ScanQueue))
}

const optionsForKey = (key) => queryOptions.find(options => String(options.queryKey) === key)

// Mutations are found by the endpoint their own mutationFn reaches rather than
// by the order they were declared in, so moving one cannot silently point a test
// at another. The probe runs the real mutationFn, so the API spies are cleared
// afterwards and the caller starts from a clean slate.
const mutationCalling = (apiFn, probe) => {
  const match = mutations.find(mutation => {
    apiFn.mockClear()
    try {
      mutation.mutationFn(probe)
    } catch {
      return false
    }
    return apiFn.mock.calls.length > 0
  })
  Object.values(api).forEach(spy => spy.mockClear())
  expect(match, 'no mutation reached that endpoint').toBeDefined()
  return match
}

const seedDetailState = ({ addSelection = null, confirmation = null } = {}) => {
  stateSeeds.length = 0
  seedsConsumed.length = 0
  stateSeeds.push(
    { name: 'addSelection', value: addSelection, setter: setAddSelection },
    { name: 'confirmation', value: confirmation, setter: setConfirmation },
  )
}

const expectSeedsWentWhereIntended = () => {
  expect(seedsConsumed, 'JobDetail no longer takes its two null states first').toEqual(['addSelection', 'confirmation'])
}

const showList = (jobs = [activeJob]) => {
  route.params = {}
  route.location = { pathname: '/scans', state: null }
  queryResults.set('scan-jobs', { data: { jobs }, isLoading: false, isError: false })
}

const showDetail = ({ fromScanQueue = false, job = jobDetail } = {}) => {
  route.params = { jobId: '7' }
  route.location = { pathname: '/scans/7', state: fromScanQueue ? { fromScanQueue: true } : null }
  queryResults.set('scan-job,7', { data: job, isLoading: false, isError: false })
}

const showUnloadableDetail = ({ fromScanQueue = false, isLoading = false, isError = false } = {}) => {
  route.params = { jobId: '7' }
  route.location = { pathname: '/scans/7', state: fromScanQueue ? { fromScanQueue: true } : null }
  queryResults.set('scan-job,7', { data: undefined, isLoading, isError })
}

let documentStub

beforeEach(() => {
  navigate.mockReset()
  queryResults.clear()
  stateSeeds.length = 0
  seedsConsumed.length = 0
  setAddSelection.mockReset()
  setConfirmation.mockReset()
  Object.values(api).forEach(spy => spy.mockReset())
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

describe('a job that cannot be shown', () => {
  it('keeps the way back to the queue while the job is still loading', () => {
    showUnloadableDetail({ isLoading: true, fromScanQueue: true })

    renderScanQueue()
    clickButton('scanner.backToScans')

    expect(navigate).toHaveBeenCalledWith(-1)
  })

  it('keeps the way back to the queue when the job is gone', () => {
    // A job is deleted server-side at its expiry (purge_expired_scan_jobs), so
    // GET /recognize/jobs/{id} answers 404 and this is the state a user returning
    // to a bookmarked or historical /scans/:jobId lands in. Before, the outer
    // modal's X, backdrop and Escape were the way out of it; the page has to
    // carry its own.
    showUnloadableDetail({ isError: true })

    const markup = renderScanQueue()
    expect(markup).toContain('scanner.jobLoadFailed')
    clickButton('scanner.backToScans')

    expect(navigate).toHaveBeenCalledWith('/scans', { replace: true })
  })

  it('offers nothing to discard until the job is known', () => {
    // The control for the two above: the header is not simply pasted onto every
    // state. Discarding a job that failed to load would delete by id on a page
    // that cannot say what it is deleting.
    const discardControls = () => rendered.filter(entry => (
      entry.type === 'button' && entry.props?.['aria-label'] === 'scanner.discardJob'
    ))

    showUnloadableDetail({ isError: true })
    renderScanQueue()
    expect(discardControls()).toHaveLength(0)

    showUnloadableDetail({ isLoading: true })
    renderScanQueue()
    expect(discardControls()).toHaveLength(0)

    showDetail()
    renderScanQueue()
    expect(discardControls()).toHaveLength(1)
  })
})

describe('the page heading', () => {
  const topLevelHeadings = markup => markup.match(/<h1[\s>]/g) || []

  it('titles the queue with exactly one first-level heading', () => {
    showList()

    const markup = renderScanQueue()

    expect(topLevelHeadings(markup)).toHaveLength(1)
    expect(markup).toContain('scanner.queueTitle')
  })

  it('titles a job the same way, in every state the job can be in', () => {
    // Heading navigation is how a screen-reader user finds where they are, and
    // the states with no job content are the ones that need it most.
    showDetail()
    expect(topLevelHeadings(renderScanQueue())).toHaveLength(1)

    showUnloadableDetail({ isLoading: true })
    expect(topLevelHeadings(renderScanQueue())).toHaveLength(1)

    showUnloadableDetail({ isError: true })
    const markup = renderScanQueue()
    expect(topLevelHeadings(markup)).toHaveLength(1)
    expect(markup).toContain('scanner.queueTitle')
  })
})

describe('reviewing the items on a job', () => {
  it('renders a review panel for every unresolved item, in order', () => {
    showDetail({ job: twoItemJob })

    renderScanQueue()

    const panels = elementsOf(ScanItemPanel)
    expect(panels).toHaveLength(twoItemJob.items.length)
    expect(panels.map(panel => panel.props.item)).toEqual(twoItemJob.items)
    expect(panels.every(panel => panel.props.jobId === twoItemJob.id)).toBe(true)
  })

  it('opens the add dialog on the candidate a panel reports', () => {
    showDetail()
    seedDetailState()

    renderScanQueue()
    expectSeedsWentWhereIntended()
    const panel = onlyElementOf(ScanItemPanel, 'review panel')
    panel.props.onAdd(jobDetail.items[0], candidate)

    expect(setAddSelection).toHaveBeenCalledWith({ item: jobDetail.items[0], match: candidate })
  })

  it('mounts the add dialog on the chosen candidate and resolves the scan with it', () => {
    // The add dialog is where the card actually enters the collection; the scan
    // has to be resolved against the same card, or the item stays in the inbox
    // and the user adds it twice.
    const item = jobDetail.items[0]
    showDetail()
    seedDetailState({ addSelection: { item, match: candidate } })

    renderScanQueue()
    expectSeedsWentWhereIntended()
    const dialog = onlyElementOf(ScanAddModal, 'add dialog')

    expect(dialog.props.match).toBe(candidate)
    // The scan's own language beats the candidate's, which is 'en' here.
    expect(dialog.props.defaultLang).toBe('de')

    dialog.props.onAdded()
    const resolve = mutationCalling(api.resolveScanJobItem, { item: { id: 0 } })
    expect(resolve.mutate).toHaveBeenCalledWith({ item, cardId: candidate.tcg_card_id })

    resolve.mutationFn({ item, cardId: candidate.tcg_card_id })
    expect(api.resolveScanJobItem).toHaveBeenCalledWith(7, item.id, candidate.tcg_card_id)

    dialog.props.onClose()
    expect(setAddSelection).toHaveBeenCalledWith(null)
  })

  it('mounts no add dialog until a candidate has been chosen', () => {
    // The control for the test above: the dialog is mounted on the selection,
    // not on the page.
    showDetail()

    renderScanQueue()

    expect(elementsOf(ScanAddModal)).toHaveLength(0)
  })
})

describe('confirming a destructive action', () => {
  it('wires the header control to the discard the page performs', () => {
    // The seam this commit introduced. The test below seeds `confirmation`
    // directly and the header test only counts the button, so neither presses
    // it: onDiscard could be a no-op and both would still pass, leaving the
    // page's only destructive control dead with a green suite.
    showDetail()
    seedDetailState()

    renderScanQueue()
    expectSeedsWentWhereIntended()
    // Found by aria-label, not by text: its only child is an icon.
    const discard = rendered.find(entry => (
      entry.type === 'button' && entry.props?.['aria-label'] === 'scanner.discardJob'
    ))
    expect(discard, 'no discard control was rendered').toBeDefined()
    discard.props.onClick({ stopPropagation: () => {} })

    expect(setConfirmation).toHaveBeenCalledWith({ type: 'discard' })
  })

  it('asks before discarding the job, and discards it on confirmation', () => {
    showDetail()
    seedDetailState({ confirmation: { type: 'discard' } })

    renderScanQueue()
    expectSeedsWentWhereIntended()
    const dialog = onlyElementOf(ConfirmDialog, 'confirmation')

    expect(dialog.props.isOpen).toBe(true)
    expect(dialog.props.destructive).toBe(true)
    expect(dialog.props.title).toBe('scanner.discardJob')
    expect(dialog.props.message).toBe('scanner.discardJobConfirm')

    dialog.props.onConfirm()

    expect(mutationCalling(api.deleteScanJob, undefined).mutate).toHaveBeenCalledTimes(1)
  })

  it('asks before dismissing a single scan, and resolves it on confirmation', () => {
    const item = jobDetail.items[0]
    showDetail()
    seedDetailState({ confirmation: { type: 'dismiss', item } })

    renderScanQueue()
    const dialog = onlyElementOf(ConfirmDialog, 'confirmation')

    expect(dialog.props.isOpen).toBe(true)
    expect(dialog.props.title).toBe('scanner.dismissScan')

    dialog.props.onConfirm()

    // No card id: dismissing deletes the photo and leaves the collection alone.
    expect(mutationCalling(api.resolveScanJobItem, { item: { id: 0 } }).mutate).toHaveBeenCalledWith({ item })
    expect(mutationCalling(api.deleteScanJob, undefined).mutate).not.toHaveBeenCalled()
  })

  it('leaves the confirmation closed, and confirming a nothing does nothing', () => {
    // The control for both tests above: the dialog is driven by the pending
    // action rather than standing open, and neither destructive path can fire
    // without one.
    showDetail()

    renderScanQueue()
    const dialog = onlyElementOf(ConfirmDialog, 'confirmation')
    expect(dialog.props.isOpen).toBe(false)

    dialog.props.onConfirm()

    expect(mutationCalling(api.deleteScanJob, undefined).mutate).not.toHaveBeenCalled()
    expect(mutationCalling(api.resolveScanJobItem, { item: { id: 0 } }).mutate).not.toHaveBeenCalled()
  })
})

describe('leaving a job that no longer exists', () => {
  const discardSucceeds = () => mutationCalling(api.deleteScanJob, undefined).onSuccess()

  const resolveSucceeds = (item) => mutationCalling(api.resolveScanJobItem, { item: { id: 0 } })
    .onSuccess(undefined, { item })

  it('pops back to the queue entry the job was opened from once it is discarded', () => {
    showDetail({ fromScanQueue: true })
    renderScanQueue()

    discardSucceeds()

    expect(navigate).toHaveBeenCalledWith(-1)
    expect(navigate).not.toHaveBeenCalledWith('/scans', { replace: true })
  })

  it('replaces a job the scanner pushed to with the queue instead', () => {
    // The control for the pop: without the marker the queue is not behind us,
    // and popping would leave the app or land on the scanner.
    showDetail()
    renderScanQueue()

    discardSucceeds()

    expect(navigate).toHaveBeenCalledWith('/scans', { replace: true })
    expect(navigate).not.toHaveBeenCalledWith(-1)
  })

  it('leaves the same way when the last item on the job is resolved', () => {
    showDetail({ fromScanQueue: true })
    renderScanQueue()

    resolveSucceeds(jobDetail.items[0])

    expect(navigate).toHaveBeenCalledWith(-1)
  })

  it('stays on the job while other items still need review', () => {
    // The control for the auto-exit: resolving one of several must not throw the
    // user out of a job they are halfway through.
    showDetail({ job: twoItemJob, fromScanQueue: true })
    renderScanQueue()

    resolveSucceeds(twoItemJob.items[0])

    expect(navigate).not.toHaveBeenCalled()
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
