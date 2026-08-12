import { Suspense, createContext, lazy, useCallback, useContext, useMemo, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { getScanJobs } from '../api/client'
import UnifiedCardScanner from '../components/UnifiedCardScanner'
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

const ScannerContext = createContext(null)

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

export function ScannerProvider({ children }) {
  // One panel at a time: null, 'scanner' or 'custom'.
  const [panel, setPanel] = useState(null)
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

  const openScanner = useCallback(() => setPanel('scanner'), [])
  const closeScanner = useCallback(() => {
    setPanel(current => (current === 'scanner' ? null : current))
  }, [])
  const openCustomCard = useCallback(() => setPanel('custom'), [])
  const closeCustomCard = useCallback(() => {
    setPanel(current => (current === 'custom' ? null : current))
  }, [])

  const runQuickAdd = useCallback(action => {
    const destination = quickAddDestination(action, location.pathname)
    if (destination) navigate(destination)
    if (action === QUICK_ADD_SCAN) setPanel('scanner')
    else if (action === QUICK_ADD_CUSTOM) setPanel('custom')
  }, [location.pathname, navigate])

  const handleCustomCreated = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['custom-cards'] })
  }, [queryClient])

  const value = useMemo(() => ({
    openScanner,
    closeScanner,
    openCustomCard,
    runQuickAdd,
    isScannerOpen: panel === 'scanner',
    scanAttention: scanAttentionCount(scanJobs),
    scansActive: hasActiveScanJobs(scanJobs),
  }), [closeScanner, openCustomCard, openScanner, panel, runQuickAdd, scanJobs])

  return (
    <ScannerContext.Provider value={value}>
      {children}
      {/* Mounted for the whole session rather than only while open: the scanner
          bumps a generation counter on every open and close so a submission
          that resolves after the user walked away cannot navigate them to the
          new job. Unmounting on close would throw that guard away. */}
      <UnifiedCardScanner isOpen={panel === 'scanner'} onClose={closeScanner} />
      {panel === 'custom' && (
        <Suspense fallback={null}>
          <CustomCardModal
            onClose={closeCustomCard}
            onCreated={handleCustomCreated}
            autoAddCollection={false}
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
