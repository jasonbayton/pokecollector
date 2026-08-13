export const SCAN_JOBS_QUERY_KEY = ['scan-jobs']

export const isScanJobActive = job => Number(job?.active || 0) > 0

export const scanJobPollInterval = (job, intervalMs = 3000) =>
  isScanJobActive(job) ? intervalMs : false

export const scanAttentionCount = (jobs = []) =>
  jobs.reduce((total, job) => total + Number(job?.attention || 0), 0)

export const hasActiveScanJobs = (jobs = []) => jobs.some(isScanJobActive)

// Every count here is optional in practice: a payload written before a counter
// existed, or one truncated by a failed refetch, leaves a field undefined. The
// bare sum then rendered the literal text "NaN remaining" at the user, which
// reads as a broken app rather than as a job whose progress is not yet known.
const finiteCount = value => {
  // `Number(value || 0)` is not enough: it rescues null and undefined but
  // turns any non-numeric string straight back into NaN, which is the exact
  // outcome this exists to prevent.
  const count = Number(value)
  return Number.isFinite(count) ? count : 0
}

export const scanJobRemaining = job => (
  finiteCount(job?.pending) + finiteCount(job?.processing) + finiteCount(job?.retrying)
)

// The set of items with a re-take or retry in flight.
//
// Kept as an explicit set driven by the mutation lifecycle rather than read
// from a mutation's `variables`. An observer exposes only its CURRENT
// variables, and TanStack Query replaces those the moment mutate() is called
// again, even while the earlier call is still pending. Deriving the gate from
// them therefore un-gated item A as soon as the user started item B, handing
// back A's stale candidates while its re-take was still in flight.
export const addBusyItem = (current, itemId) => (
  current.includes(itemId) ? current : [...current, itemId]
)

export const removeBusyItem = (current, itemId) => current.filter(
  candidate => candidate !== itemId,
)
