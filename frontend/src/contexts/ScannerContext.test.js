import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { SCAN_JOBS_QUERY_KEY } from '../utils/scanJobs'
import UnifiedCardScanner from '../components/UnifiedCardScanner'
import { CustomCardModal } from '../components/CardItem'
import {
  QUICK_ADD_CUSTOM,
  QUICK_ADD_QUEUE,
  QUICK_ADD_SCAN,
  QUICK_ADD_SEARCH,
  ScannerProvider,
  quickAddDestination,
  quickAddHiddenOn,
  quickAddPanel,
  useScanner,
} from './ScannerContext'

const { useQuery, invalidateQueries, navigate, apiStubs, panelSeed, panelWrites } = vi.hoisted(() => ({
  useQuery: vi.fn(),
  invalidateQueries: vi.fn(),
  navigate: vi.fn(),
  apiStubs: { getScanJobs: vi.fn(), enqueueScanJob: vi.fn() },
  panelSeed: { value: null },
  panelWrites: [],
}))

// The provider keeps its whole visible state in one object, so it can be
// recognised by its initial value and nothing else in the render - the router's
// own state included - is touched. Seeding it is how a server render, where a
// state update after the render is a no-op, can be shown the provider as the
// user would find it mid-session; recording the writes is how an action's
// effect on that state can be read back.
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useState: initial => {
      if (!initial || typeof initial !== 'object' || !('panel' in initial)) return actual.useState(initial)
      const [value, set] = actual.useState({ ...initial, ...(panelSeed.value || {}) })
      return [value, next => {
        panelWrites.push(typeof next === 'function' ? next(value) : next)
        set(next)
      }]
    },
  }
})

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
  panelSeed.value = null
  panelWrites.length = 0
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

describe('quickAddPanel', () => {
  it('names the panel each action opens', () => {
    expect(quickAddPanel(QUICK_ADD_SCAN)).toBe('scanner')
    expect(quickAddPanel(QUICK_ADD_CUSTOM)).toBe('custom')
  })

  it('opens no panel for the two actions that only move the user', () => {
    expect(quickAddPanel(QUICK_ADD_SEARCH)).toBeNull()
    expect(quickAddPanel(QUICK_ADD_QUEUE)).toBeNull()
  })
})

describe('quickAddHiddenOn', () => {
  it('hides the control on the scan queue, which draws its own backdrop over it', () => {
    expect(quickAddHiddenOn('/scans')).toBe(true)
    expect(quickAddHiddenOn('/scans/12')).toBe(true)
  })

  it('leaves it on every other page', () => {
    ['/', '/search', '/collection', '/sets', '/binders/3', '/settings'].forEach(pathname => {
      expect(quickAddHiddenOn(pathname)).toBe(false)
    })
  })
})

describe('ScannerProvider', () => {
  it('opens the scanner without leaving the page the user is on', () => {
    const markup = render({ entries: ['/collection'] })

    scanner.openScanner()

    // No navigation at all: the page below keeps its component instances, and
    // with them its own state.
    expect(navigate).not.toHaveBeenCalled()
    expect(lastPanel()).toBe('scanner')
    expect(markup).toContain('<p>page content</p>')
  })

  it('opens the scanner when quick add asks for a scan', () => {
    render({ entries: ['/collection'] })

    scanner.runQuickAdd(QUICK_ADD_SCAN)

    expect(lastPanel()).toBe('scanner')
    expect(navigate).not.toHaveBeenCalled()
  })

  it('opens the manual card form when quick add asks for one', () => {
    render({ entries: ['/collection'] })

    scanner.runQuickAdd(QUICK_ADD_CUSTOM)

    expect(lastPanel()).toBe('custom')
    expect(navigate).not.toHaveBeenCalled()
  })

  it('opens the scanner from the card search without touching its URL state', () => {
    render({ entries: ['/search?q=charizard&rarity=Rare&page=3'] })

    scanner.runQuickAdd(QUICK_ADD_SCAN)

    expect(navigate).not.toHaveBeenCalled()
    expect(lastPanel()).toBe('scanner')
  })

  it('still walks to the catalogue and the queue from elsewhere, opening nothing', () => {
    render({ entries: ['/collection'] })

    scanner.runQuickAdd(QUICK_ADD_SEARCH)
    scanner.runQuickAdd(QUICK_ADD_QUEUE)

    expect(navigate).toHaveBeenNthCalledWith(1, '/search')
    expect(navigate).toHaveBeenNthCalledWith(2, '/scans')
    expect(panelWrites).toHaveLength(0)
  })

  it('closes only the panel the caller named', () => {
    panelSeed.value = { panel: 'custom', scannerMounted: true }
    render()

    // A scanner close handler left over from a moment ago must not shut the
    // manual card form the user has since opened.
    scanner.closeScanner()

    expect(lastPanel()).toBe('custom')
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

  it('tells the page below which of its panels owns the screen', () => {
    // The card search suspends its arrow-key pagination on these two. It has no
    // other way to know either panel is open.
    panelSeed.value = { panel: 'custom', scannerMounted: false }
    render()
    expect(scanner.isCustomCardOpen).toBe(true)
    expect(scanner.isScannerOpen).toBe(false)

    panelSeed.value = { panel: 'scanner', scannerMounted: true }
    render()
    expect(scanner.isScannerOpen).toBe(true)
    expect(scanner.isCustomCardOpen).toBe(false)
  })

  it('mounts the real scanner, open, while the scanner panel owns the screen', async () => {
    panelSeed.value = { panel: 'scanner', scannerMounted: true }

    const mount = scannerMount(scannerProviderTree())

    expect(mount).toBeDefined()
    expect(mount.props.isOpen).toBe(true)
    expect(typeof mount.props.onClose).toBe('function')
    expect(await loadLazy(mount.type)).toBe(UnifiedCardScanner)
  })

  it('keeps the scanner mounted, closed, once it has been opened', async () => {
    // It bumps a generation counter on open and close so a batch that finishes
    // uploading after the user walked away cannot drag them to the new job.
    // Unmounting on close would throw that guard away.
    panelSeed.value = { panel: null, scannerMounted: true }

    const mount = scannerMount(scannerProviderTree())

    expect(mount).toBeDefined()
    expect(mount.props.isOpen).toBe(false)
    expect(await loadLazy(mount.type)).toBe(UnifiedCardScanner)
  })

  it('loads nothing of the scanner before the first open', () => {
    // App.jsx imports this module for its provider, so anything imported here
    // statically ships in the chunk the login screen and the public /u pages
    // load. The scanner is far too big to ride along for a page that cannot
    // use it.
    const tree = scannerProviderTree()

    expect(scannerMount(tree)).toBeUndefined()
    expect(tree.some(node => node.type === 'p')).toBe(true)
  })

  it('opens the manual card form with the step that puts the card in the collection', async () => {
    // Quick add is an add-to-collection control. Creating the card and stopping
    // there leaves the user on a page that shows no sign of it.
    panelSeed.value = { panel: 'custom', scannerMounted: false }

    const mount = customCardMount(scannerProviderTree())

    expect(mount).toBeDefined()
    expect(mount.props.autoAddCollection).toBe(true)
    expect(typeof mount.props.onClose).toBe('function')
    expect(await loadLazy(mount.type)).toBe(CustomCardModal)
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

const lastPanel = () => (panelWrites.length ? panelWrites[panelWrites.length - 1].panel : undefined)

// The two deferred panels are told apart by the props the provider gives them,
// which is also what each assertion is about; loadLazy then pins which module
// the mount actually resolves to.
const scannerMount = tree => tree.find(node => node.props && 'isOpen' in node.props)
const customCardMount = tree => tree.find(node => node.props && 'autoAddCollection' in node.props)

// React resolves a lazy element by calling _init(_payload), which throws the
// pending import the first time. That is the contract React itself uses.
async function loadLazy(type) {
  try {
    return type._init(type._payload)
  } catch (thrown) {
    if (!thrown || typeof thrown.then !== 'function') throw thrown
    await thrown
    return type._init(type._payload)
  }
}

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
