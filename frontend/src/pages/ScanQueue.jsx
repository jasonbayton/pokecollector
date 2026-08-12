import { useEffect, useState } from 'react'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Clock3, Loader2, ScanLine, Trash2 } from 'lucide-react'
import toast from 'react-hot-toast'
import {
  deleteScanJob,
  getScanJob,
  getScanJobs,
  resolveScanJobItem,
  retryScanJobItem,
} from '../api/client'
import ScanAddModal from '../components/ScanAddModal'
import { ScanItemPanel } from '../components/ScanReview'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import { useSettings } from '../contexts/SettingsContext'
import {
  SCAN_JOBS_QUERY_KEY,
  hasActiveScanJobs,
  isScanJobActive,
  scanJobPollInterval,
} from '../utils/scanJobs'
import { formatRetryCountdown } from '../utils/retryCountdown'

// react-router stamps an incrementing `idx` onto window.history.state, starting
// at 0 for the entry the tab was loaded on. An idx above 0 therefore means this
// router pushed at least one entry before we arrived, so going back lands on one
// of our own pages instead of leaving the app. location.key cannot answer this:
// replacing the first entry mints a fresh key while the index stays 0.
export function hasInAppPredecessor(historyState) {
  return Number.isInteger(historyState?.idx) && historyState.idx > 0
}

function currentHistoryState() {
  if (typeof window === 'undefined') return null
  return window.history?.state ?? null
}

function useRetryClock(enabled) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!enabled) return undefined
    setNow(Date.now())
    const interval = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(interval)
  }, [enabled])

  return now
}

function expiryLabel(job, t) {
  if (!job?.expires_at) return ''
  return `${t('scanner.expiresOn')} ${new Date(job.expires_at).toLocaleDateString()}`
}

function JobRow({ job, onOpen, retryNow, t }) {
  const waitingOnly = Number(job.retrying || 0) > 0 && Number(job.active || 0) === Number(job.retrying || 0)
  return (
    <button type="button" onClick={() => onOpen(job.id)}
      className="w-full rounded-2xl border border-border bg-bg-surface p-4 text-left transition-colors hover:border-brand-red/40">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-bold text-text-primary">
            {job.processed}/{job.total} {t('scanner.processed')}
          </p>
          <p className="mt-1 text-xs text-text-muted">
            {job.attention > 0 && `${job.attention} ${t('scanner.needReview')}`}
            {job.attention > 0 && job.failed_attention > 0 && ' · '}
            {job.failed_attention > 0 && `${job.failed_attention} ${t('scanner.failed')}`}
          </p>
          <p className="mt-1 flex items-center gap-1 text-[11px] text-text-muted">
            <Clock3 size={11} /> {expiryLabel(job, t)}
          </p>
        </div>
        {waitingOnly ? (
          <span className="flex flex-shrink-0 items-center gap-1.5 text-xs text-text-muted">
            <Clock3 size={13} /> {formatRetryCountdown(job.next_retry_at, t, retryNow, job.retry_reason)}
          </span>
        ) : isScanJobActive(job) ? (
          <span className="flex flex-shrink-0 items-center gap-1.5 text-xs text-text-muted">
            <Loader2 size={13} className="animate-spin" /> {t('scanner.processing')}
          </span>
        ) : (
          <span className="rounded-full bg-brand-red/15 px-2 py-1 text-[10px] font-black uppercase tracking-wider text-brand-red">
            {job.attention} {t('scanner.ready')}
          </span>
        )}
      </div>
    </button>
  )
}

// The outer Modal used to supply the X, the backdrop and Escape to every state
// this component could be in. Nothing replaced that for the two states that hold
// no job content to navigate from: a job is purged server-side at its expiry, so
// the next GET answers 404 and the page became a bare error box with no control
// on it at all. The way back therefore lives in the shell, above the branch, and
// the heading with it - the Modal's own h2 was the page's only heading.
function JobDetailShell({ onBack, onDiscard, isDiscarding, t, children }) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3">
        <button type="button" onClick={onBack}
          className="btn-ghost px-3 py-1.5 text-sm">
          <ArrowLeft size={16} /> {t('scanner.backToScans')}
        </button>
        {onDiscard && (
          <button type="button" onClick={onDiscard} disabled={isDiscarding}
            className="btn-ghost h-9 w-9 border-brand-red/30 p-0 text-brand-red hover:bg-brand-red/10"
            aria-label={t('scanner.discardJob')} title={t('scanner.discardJob')}>
            <Trash2 size={17} />
          </button>
        )}
      </div>
      <h1 className="text-xl font-bold text-text-primary">{t('scanner.queueTitle')}</h1>
      {children}
    </div>
  )
}

function JobDetail({ jobId }) {
  const { t } = useSettings()
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const [addSelection, setAddSelection] = useState(null)
  const [confirmation, setConfirmation] = useState(null)

  // The list is not always the previous entry: the scanner pushes straight to a
  // freshly enqueued job from the search page. Only a detail opened from a queue
  // row may pop; anything else replaces itself with the list, which keeps the
  // history index where it was so leaving the list still has a sane target.
  const backToList = () => {
    if (location.state?.fromScanQueue) navigate(-1)
    else navigate('/scans', { replace: true })
  }

  const { data: job, isLoading, isError } = useQuery({
    queryKey: ['scan-job', jobId],
    queryFn: () => getScanJob(jobId),
    refetchInterval: query => scanJobPollInterval(query.state.data),
  })
  const retryNow = useRetryClock(Number(job?.retrying || 0) > 0)

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['scan-job', jobId] })
    queryClient.invalidateQueries({ queryKey: SCAN_JOBS_QUERY_KEY })
  }

  const resolveMutation = useMutation({
    mutationFn: ({ item, cardId = null }) => resolveScanJobItem(jobId, item.id, cardId),
    onSuccess: (_data, { item }) => {
      const remaining = (job?.items || []).filter(candidate => candidate.id !== item.id)
      setConfirmation(null)
      invalidate()
      if (remaining.length === 0) backToList()
    },
    onError: error => toast.error(error?.response?.data?.detail || t('scanner.actionFailed')),
  })

  const retryMutation = useMutation({
    mutationFn: item => retryScanJobItem(jobId, item.id),
    onSuccess: invalidate,
    onError: error => toast.error(error?.response?.data?.detail || t('scanner.actionFailed')),
  })

  const deleteMutation = useMutation({
    mutationFn: () => deleteScanJob(jobId),
    onSuccess: () => {
      setConfirmation(null)
      invalidate()
      backToList()
    },
    onError: error => toast.error(error?.response?.data?.detail || t('scanner.actionFailed')),
  })

  const dismiss = item => setConfirmation({ type: 'dismiss', item })

  const discardJob = () => setConfirmation({ type: 'discard' })

  const confirmDestructiveAction = () => {
    if (confirmation?.type === 'dismiss') resolveMutation.mutate({ item: confirmation.item })
    else if (confirmation?.type === 'discard') deleteMutation.mutate()
  }

  // No job to discard yet, so the shell renders the way back on its own.
  if (isLoading) {
    return (
      <JobDetailShell onBack={backToList} t={t}>
        <div className="flex justify-center py-16"><Loader2 size={28} className="animate-spin text-brand-red" /></div>
      </JobDetailShell>
    )
  }
  if (isError || !job) {
    return (
      <JobDetailShell onBack={backToList} t={t}>
        <div role="alert" className="rounded-xl border border-brand-red/20 bg-brand-red/10 p-4 text-center text-sm text-brand-red">
          {t('scanner.jobLoadFailed')}
        </div>
      </JobDetailShell>
    )
  }

  return (
    <JobDetailShell onBack={backToList} onDiscard={discardJob} isDiscarding={deleteMutation.isPending} t={t}>
      <div className="rounded-2xl border border-border bg-bg-surface p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="font-bold text-text-primary">{job.processed}/{job.total} {t('scanner.processed')}</p>
            <p className="mt-1 text-xs text-text-muted">{expiryLabel(job, t)}</p>
          </div>
          {Number(job.retrying || 0) > 0 && Number(job.active || 0) === Number(job.retrying || 0) ? (
            <span className="flex items-center gap-1.5 text-xs text-text-muted">
              <Clock3 size={13} /> {formatRetryCountdown(job.next_retry_at, t, retryNow, job.retry_reason)}
            </span>
          ) : isScanJobActive(job) && (
            <span className="flex items-center gap-1.5 text-xs text-text-muted">
              <Loader2 size={13} className="animate-spin" /> {t('scanner.processing')}
            </span>
          )}
        </div>
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-white/5">
          <div className="h-full rounded-full bg-brand-red transition-all"
            style={{ width: `${job.total ? Math.round((job.processed / job.total) * 100) : 0}%` }} />
        </div>
        <p className="mt-2 text-xs text-text-muted">
          {job.pending + job.processing + job.retrying} {t('scanner.remaining')}
          {job.failed > 0 && ` · ${job.failed} ${t('scanner.failed')}`}
        </p>
      </div>

      <div className="space-y-3">
        {(job.items || []).map(item => (
          <ScanItemPanel
            key={item.id}
            jobId={job.id}
            item={item}
            onAdd={(scanItem, match) => setAddSelection({ item: scanItem, match })}
            onRetry={itemToRetry => retryMutation.mutate(itemToRetry)}
            onDismiss={dismiss}
            retryNow={retryNow}
            t={t}
          />
        ))}
      </div>

      {addSelection && (
        <ScanAddModal
          match={addSelection.match}
          defaultLang={addSelection.item.recognized?.language || addSelection.match.lang || 'en'}
          onClose={() => setAddSelection(null)}
          onAdded={() => {
            resolveMutation.mutate({
              item: addSelection.item,
              cardId: addSelection.match.tcg_card_id,
            })
            setAddSelection(null)
          }}
        />
      )}

      <ConfirmDialog
        isOpen={Boolean(confirmation)}
        onClose={() => setConfirmation(null)}
        onConfirm={confirmDestructiveAction}
        title={confirmation?.type === 'discard' ? t('scanner.discardJob') : t('scanner.dismissScan')}
        message={confirmation?.type === 'discard' ? t('scanner.discardJobConfirm') : t('scanner.dismissScanConfirm')}
        confirmLabel={confirmation?.type === 'discard' ? t('scanner.discardJob') : t('scanner.dismissScan')}
        cancelLabel={t('common.cancel')}
        isPending={deleteMutation.isPending || resolveMutation.isPending}
        destructive
      />
    </JobDetailShell>
  )
}

export default function ScanQueue() {
  const { t } = useSettings()
  const navigate = useNavigate()
  const { jobId } = useParams()

  const { data, isLoading } = useQuery({
    queryKey: SCAN_JOBS_QUERY_KEY,
    queryFn: getScanJobs,
    refetchInterval: query => hasActiveScanJobs(query.state.data?.jobs || []) ? 3000 : false,
  })

  // Leaving the queue is a page navigation now, so it has to respect where the
  // user came from. Only a direct load (or an arrival from outside the app) has
  // nowhere to go back to, and then the search page is the one that hosts the
  // scanner and the queue's own entry point.
  const leaveScans = () => {
    if (hasInAppPredecessor(currentHistoryState())) navigate(-1)
    else navigate('/search')
  }

  const jobs = data?.jobs || []
  const retryNow = useRetryClock(jobs.some(job => Number(job.retrying || 0) > 0))
  return (
    <div className="mx-auto w-full max-w-4xl space-y-4 py-2">
      {jobId ? (
        <JobDetail jobId={Number(jobId)} />
      ) : (
        <>
          <div>
            <button type="button" onClick={leaveScans} className="btn-ghost px-3 py-1.5 text-sm">
              <ArrowLeft size={16} /> {t('common.back')}
            </button>
          </div>
          {/* The page carried no heading at any level once the Modal's h2 went,
              so screen-reader heading navigation found nothing on the route. */}
          <div>
            <h1 className="text-xl font-bold text-text-primary">{t('scanner.queueTitle')}</h1>
            <p className="mt-1 text-sm text-text-secondary">{t('scanner.queueSubtitle')}</p>
          </div>

          {isLoading ? (
            <div className="flex justify-center py-16"><Loader2 size={28} className="animate-spin text-brand-red" /></div>
          ) : jobs.length === 0 ? (
            <div className="card space-y-3 py-12 text-center">
              <ScanLine size={28} className="mx-auto text-text-muted opacity-50" />
              <p className="text-sm text-text-muted">{t('scanner.noScans')}</p>
              {/* Deliberately the search page rather than "wherever you came
                  from": the scanner is only reachable from the camera button
                  there, so history could drop the user somewhere with no way to
                  scan at all. It still costs the user a second click, which only
                  a scanner route or provider can remove. */}
              <button type="button" onClick={() => navigate('/search')} className="btn-primary mx-auto justify-center">
                {t('scanner.goScan')}
              </button>
            </div>
          ) : (
            <div className="space-y-2">
              {jobs.map(job => (
                <JobRow
                  key={job.id}
                  job={job}
                  // The marker is what lets the detail's back button pop instead
                  // of pushing another list entry on top of this one.
                  onOpen={id => navigate(`/scans/${id}`, { state: { fromScanQueue: true } })}
                  retryNow={retryNow}
                  t={t}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
