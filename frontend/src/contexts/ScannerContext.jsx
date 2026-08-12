import { Suspense, createContext, lazy, useCallback, useContext, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { getScanJobs } from '../api/client'
import { useSettings } from './SettingsContext'
import {
  SCAN_JOBS_QUERY_KEY,
  hasActiveScanJobs,
  scanAttentionCount,
} from '../utils/scanJobs'

// Only pulled in when the user actually asks for a manual card. CardItem carries
// the whole card modal with it, and the quick-add control is mounted on every
// authenticated page including the home screen.
const CustomCardModal = lazy(() => import('../components/CardItem').then(module => ({
  default: module.CustomCardModal,
})))

// Likewise deferred. App.jsx imports this module for its provider, so a static
// import here put the scanner, its confirm dialog and its sheet into the chunk
// the entry point loads - which is every visit, including the login screen and
// the public /u share pages, neither of which can scan anything.
const UnifiedCardScanner = lazy(() => import('../components/UnifiedCardScanner'))

const ScannerContext = createContext(null)

function ScannerLoadingFallback() {
  const { t } = useSettings()
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6 md:bg-black/80 md:backdrop-blur-sm"
    >
      <div className="flex items-center gap-3 rounded-2xl border border-border bg-bg-surface px-5 py-4 text-sm font-medium text-text-primary shadow-2xl">
        <span className="h-5 w-5 animate-spin rounded-full border-2 border-brand-red border-t-transparent" aria-hidden="true" />
        {t('scanner.opening')}
      </div>
    </div>
  )
}

export const QUICK_ADD_SCAN = 'scan'
export const QUICK_ADD_SEARCH = 'search'
export const QUICK_ADD_CUSTOM = 'custom'
export const QUICK_ADD_QUEUE = 'queue'

// Which route a quick-add action needs, or null when the current location is
// already that route. Navigating to the path we are standing on replaces the
// current search string, and the card search keeps its query and its filters
// there, so re-entering /search from /search would silently wipe the search the
// user is looking at. Scanning and manual creation open a dialog above whatever
// page is showing, so they never need a route at all.
export function quickAddDestination(action, pathname) {
  if (action === QUICK_ADD_SEARCH) return pathname === '/search' ? null : '/search'
  if (action === QUICK_ADD_QUEUE) return pathname === '/scans' ? null : '/scans'
  return null
}

// Which panel a quick-add action opens over the current page, or null when the
// action only moves the user. Exported so the control's promise - "scan card"
// opens the scanner, "create card manually" opens the manual card form - is a
// fact a test can hold the provider to.
export function quickAddPanel(action) {
  if (action === QUICK_ADD_SCAN) return 'scanner'
  if (action === QUICK_ADD_CUSTOM) return 'custom'
  return null
}

// The scan queue is a route that renders itself as a modal, so it draws its own
// full-screen backdrop over the page. The quick-add control sits below the
// dialog layer by design, which on these two routes leaves it visible through
// the backdrop but unclickable: the click lands on the backdrop, and the queue
// closes itself by navigating to /search. A control that cannot be used and
// throws the user off the page they are on is worse than no control, so it is
// not drawn here. Quick add is one dismissal away - closing the queue lands on
// the card search, which has it.
export function quickAddHiddenOn(pathname) {
  return pathname === '/scans' || pathname.startsWith('/scans/')
}

// The provider's whole visible state, in one object so it moves atomically:
// which panel owns the screen, and whether the scanner has been mounted yet.
// scannerMounted is set by the first open and never cleared - see the mount.
const CLOSED = { panel: null, scannerMounted: false }

export function ScannerProvider({ children }) {
  const [{ panel, scannerMounted }, setPanelState] = useState(CLOSED)
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()

  // The single queue query for the whole app. It lives here because the badge
  // now rides on the global control, and a page-level copy would poll the same
  // endpoint a second time.
  const { data: scanData } = useQuery({
    queryKey: SCAN_JOBS_QUERY_KEY,
    queryFn: getScanJobs,
    refetchInterval: query => hasActiveScanJobs(query.state.data?.jobs || []) ? 3000 : false,
  })
  const scanJobs = scanData?.jobs || []

  const showPanel = useCallback(next => setPanelState(current => ({
    panel: next,
    scannerMounted: current.scannerMounted || next === 'scanner',
  })), [])

  // Closing names the panel it closes, so a stale handler cannot shut the panel
  // that replaced it.
  const closePanel = useCallback(kind => setPanelState(current => (
    current.panel === kind ? { ...current, panel: null } : current
  )), [])

  const openScanner = useCallback(() => showPanel('scanner'), [showPanel])
  const closeScanner = useCallback(() => closePanel('scanner'), [closePanel])
  const openCustomCard = useCallback(() => showPanel('custom'), [showPanel])
  const closeCustomCard = useCallback(() => closePanel('custom'), [closePanel])

  const runQuickAdd = useCallback(action => {
    const destination = quickAddDestination(action, location.pathname)
    if (destination) navigate(destination)
    const next = quickAddPanel(action)
    if (next) showPanel(next)
  }, [location.pathname, navigate, showPanel])

  const handleCustomCreated = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['custom-cards'] })
  }, [queryClient])

  const value = useMemo(() => ({
    openScanner,
    closeScanner,
    openCustomCard,
    runQuickAdd,
    isScannerOpen: panel === 'scanner',
    // The card search suppresses its arrow-key pagination while something owns
    // the screen. Its own modals it knows about; the two this provider opens
    // over it, it can only know from here.
    isCustomCardOpen: panel === 'custom',
    scanAttention: scanAttentionCount(scanJobs),
    scansActive: hasActiveScanJobs(scanJobs),
  }), [closeScanner, openCustomCard, openScanner, panel, runQuickAdd, scanJobs])

  return (
    <ScannerContext.Provider value={value}>
      {children}
      {/* Mounted from the first open until the session ends rather than only
          while open: the scanner bumps a generation counter on every open and
          close so a submission that resolves after the user walked away cannot
          navigate them to the new job. Unmounting on close would throw that
          guard away. Before the first open there is nothing to guard, and not
          mounting keeps its chunk off every page that never scans. */}
      {(scannerMounted || panel === 'scanner') && (
        <Suspense fallback={<ScannerLoadingFallback />}>
          <UnifiedCardScanner isOpen={panel === 'scanner'} onClose={closeScanner} />
        </Suspense>
      )}
      {panel === 'custom' && (
        <Suspense fallback={null}>
          <CustomCardModal
            onClose={closeCustomCard}
            onCreated={handleCustomCreated}
            /* Quick add is an add-to-collection control. Creating the card and
               stopping there would leave the user on a page that shows no sign
               of it: the manual card exists in the catalogue only. */
            autoAddCollection
          />
        </Suspense>
      )}
    </ScannerContext.Provider>
  )
}

export function useScanner() {
  const scanner = useContext(ScannerContext)
  if (!scanner) throw new Error('useScanner must be used within ScannerProvider')
  return scanner
}
