import { useEffect, useState } from 'react'
import { Camera, Loader2, Maximize2, RefreshCw, Sparkles, Trash2 } from 'lucide-react'
import { fetchScanJobItemImage } from '../api/client'
import { CardDisplay } from './card-system'
import Modal from './ui/Modal'
import { tcgdexLanguageLabel } from '../utils/tcgdexLanguages'
import { formatRetryCountdown } from '../utils/retryCountdown'
import ScanManualPicker from './ScanManualPicker'

export function ScanZoomModal({ photoUrl, card, onClose, t }) {
  const candidateImage = card?.image_hd
    || card?.image?.replace('/low.webp', '/high.webp')
    || card?.image

  return (
    <Modal isOpen onClose={onClose} title={t('scanner.compareCandidate')} size="xl">
      <div className="flex min-h-0 flex-col items-center justify-center gap-4 p-4 md:flex-row md:gap-8 md:p-6">
        {photoUrl && (
          <figure className="flex min-h-0 min-w-0 flex-1 flex-col items-center">
            <img src={photoUrl} alt={t('scanner.yourPhoto')}
              className="max-h-[58vh] max-w-full rounded-xl object-contain md:max-h-[68vh]" />
            <figcaption className="mt-2 text-xs text-text-muted">{t('scanner.yourPhoto')}</figcaption>
          </figure>
        )}
        {candidateImage && (
          <figure className="flex min-h-0 min-w-0 flex-1 flex-col items-center">
            <img src={candidateImage} alt={card?.name}
              className="max-h-[58vh] max-w-full rounded-xl object-contain md:max-h-[68vh]" />
            <figcaption className="mt-2 text-xs text-text-muted">{card?.name}</figcaption>
          </figure>
        )}
      </div>
    </Modal>
  )
}

export function useScanItemPhoto(jobId, item) {
  const [url, setUrl] = useState(null)

  useEffect(() => {
    if (!item.has_image) {
      setUrl(null)
      return undefined
    }
    let disposed = false
    let objectUrl = null
    fetchScanJobItemImage(jobId, item.id)
      .then(nextUrl => {
        if (disposed) {
          URL.revokeObjectURL(nextUrl)
          return
        }
        objectUrl = nextUrl
        setUrl(nextUrl)
      })
      .catch(() => {
        // Guarded like the success path. Without this, a superseded fetch that
        // rejects later clears the URL the newer one already set, blanking a
        // preview that loaded perfectly well. A re-take makes exactly that
        // ordering likely, because the old file is deleted once it lands.
        if (disposed) return
        setUrl(null)
      })
    return () => {
      disposed = true
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
    // image_token, not just the id: a re-take replaces the stored file while
    // the id and has_image both stay put, so without it this effect never
    // re-ran and the panel showed the photo the user had just replaced.
  }, [jobId, item.id, item.has_image, item.image_token])

  return url
}

// Only the matcher can say which candidate it chose, and only when it was
// confident. Rank order is not that answer: the list is always sorted, so the
// first card would wear the badge even when nothing was decided.
//
// The comparison is against match.id, not match.tcg_card_id. One card can be
// listed once per language searched, so several candidates share a
// tcg_card_id and matching on it badges all of them; match.id carries the
// language and is unique within the list.
function isSuggestedMatch(item, match) {
  if (item?.identity_confident !== true) return false
  const suggested = item?.suggested_match_id
  return Boolean(suggested) && match?.id === suggested
}

function CandidateGrid({ item, matches, photoUrl, onSelect, t }) {
  const [zoomCard, setZoomCard] = useState(null)
  if (!matches?.length) return null

  return (
    <>
      {zoomCard && (
        <ScanZoomModal photoUrl={photoUrl} card={zoomCard} onClose={() => setZoomCard(null)} t={t} />
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
        {matches.map(match => {
          const language = match.lang || match._lang || 'en'
          const suggested = isSuggestedMatch(item, match)
          return (
            <div key={`${match.id}-${language}`}>
              <CardDisplay
                variant="selectable"
                card={match}
                image={match.image}
                languageLabel={tcgdexLanguageLabel(language)}
                onClick={() => onSelect(match)}
                onSelect={() => onSelect(match)}
                // The badge rides on the artwork rather than above the card, so
                // marking one candidate cannot push it out of line with the
                // rest of the grid. Bottom left keeps it clear of the state
                // indicators along the top and of the compare button.
                //
                // pointer-events-none is load-bearing, not decoration: the
                // badge is painted at z-30 over the full-bleed select button at
                // z-25, so without it the badge eats the click and tapping the
                // thing labelled "Suggested" selects nothing. The design
                // system's own .unified-card-selection marker does the same.
                overlay={(suggested || match.image) ? (
                  <>
                    {suggested && (
                      <p className="pointer-events-none absolute bottom-2 left-2 z-30 inline-flex items-center gap-1 rounded-full border border-brand-red/40 bg-black/80 px-2 py-0.5 text-[10px] font-black uppercase tracking-[0.14em] text-brand-red shadow-lg">
                        <Sparkles size={11} /> {t('scanner.suggestedMatch')}
                      </p>
                    )}
                    {match.image && (
                      <button type="button" onClick={event => {
                        event.stopPropagation()
                        setZoomCard(match)
                      }}
                        aria-label={t('scanner.compareCandidate')}
                        title={t('scanner.compareCandidate')}
                        className="absolute right-2 top-2 z-30 grid h-8 w-8 place-items-center rounded-lg border border-white/15 bg-black/75 text-white shadow-lg transition-colors hover:bg-black focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-red">
                        <Maximize2 size={15} />
                      </button>
                    )}
                  </>
                ) : null}
              />
            </div>
          )
        })}
      </div>
    </>
  )
}

export function ScanItemPanel({ jobId, item, onAdd, onRetry, onRetake, onDismiss, retryNow, isBusy = false, t }) {
  const photoUrl = useScanItemPhoto(jobId, item)
  const [photoExpanded, setPhotoExpanded] = useState(false)
  const [manualPickerOpen, setManualPickerOpen] = useState(false)
  // isBusy covers the gap between submitting a re-take or a retry and the
  // refetch that reports the item as pending again. In that window the server
  // has already reset the scan, so the candidates on screen belong to a photo
  // that is no longer attached to it. ScanAddModal writes to the collection
  // before the scan is resolved, so acting on one added the wrong card and
  // then failed to resolve with a 409.
  const active = isBusy || ['pending', 'processing', 'retrying'].includes(item.status)
  const noMatches = !isBusy && item.status === 'done' && !item.matches?.length

  return (
    <article className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
      {photoExpanded && (
        <ScanZoomModal photoUrl={photoUrl} onClose={() => setPhotoExpanded(false)} t={t} />
      )}
      {manualPickerOpen && (
        <ScanManualPicker
          defaultLang={item.recognized?.language || 'en'}
          onSelect={match => {
            setManualPickerOpen(false)
            onAdd(item, match)
          }}
          onClose={() => setManualPickerOpen(false)}
          t={t}
        />
      )}
      <div className="flex gap-4">
        <button type="button" onClick={() => {
          if (!photoUrl) return
          setPhotoExpanded(true)
        }} disabled={!photoUrl}
          className="grid aspect-[2.5/3.5] w-24 flex-shrink-0 place-items-center overflow-hidden rounded-xl border border-white/10 bg-bg-primary/50 disabled:cursor-default">
          {photoUrl
            ? <img src={photoUrl} alt={t('scanner.yourPhoto')} className="h-full w-full object-contain" />
            : <Camera size={28} className="text-text-muted opacity-50" />}
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[10px] font-black uppercase tracking-[0.16em] text-text-muted">
                {t('scanner.photoNumber')} {item.position + 1}
              </p>
              {item.recognized?.name && (
                <p className="truncate text-base font-bold text-white">{item.recognized.name}</p>
              )}
              {item.recognized?.number && (
                <p className="text-xs text-text-muted">Nr. {item.recognized.number}</p>
              )}
            </div>
            {!active && (
              <button type="button" onClick={() => onDismiss(item)}
                className="btn-ghost flex-shrink-0 border-brand-red/30 px-2 py-1 text-xs text-brand-red hover:bg-brand-red/10">
                <Trash2 size={14} /> {t('scanner.dismissScan')}
              </button>
            )}
          </div>

          {active && (
            <p className="mt-3 flex items-center gap-2 text-sm text-text-muted">
              <Loader2 size={14} className="animate-spin" />
              {item.status === 'retrying'
                ? formatRetryCountdown(item.next_attempt_at, t, retryNow, item.retry_reason)
                : t('scanner.itemProcessing')}
            </p>
          )}

          {(item.status === 'failed' || noMatches) && (
            <div className="mt-3 space-y-3">
              <p role="alert" className={`rounded-xl border px-3 py-2 text-sm ${
                item.status === 'failed'
                  ? 'border-brand-red/20 bg-brand-red/10 text-brand-red'
                  : 'border-border bg-bg-card text-text-muted'
              }`}>
                {item.error || t(noMatches ? 'scanner.noMatches' : 'scanner.recognitionFailed')}
              </p>
              <button type="button" onClick={() => onRetry(item)} disabled={!item.has_image || isBusy}
                className="btn-secondary justify-center">
                <RefreshCw size={14} /> {t('scanner.retryIndividually')}
              </button>
              <button type="button" onClick={() => setManualPickerOpen(true)} className="btn-secondary justify-center">
                {t('scanner.manualPick')}
              </button>
            </div>
          )}

          {!item.resolved && (
            <button type="button" onClick={() => onRetake?.(item)} disabled={active}
              className="btn-secondary mt-3 justify-center">
              <Camera size={14} /> {t('scanner.retakePhoto')}
            </button>
          )}
        </div>
      </div>

      {!isBusy && item.status === 'done' && item.matches?.length > 0 && (
        <div className="mt-4">
          <p className="mb-3 text-[10px] font-black uppercase tracking-[0.2em] text-text-muted">
            {t('scanner.bestMatches')} ({item.matches.length})
          </p>
          <CandidateGrid item={item} matches={item.matches} photoUrl={photoUrl} onSelect={match => onAdd(item, match)} t={t} />
          <button type="button" onClick={() => setManualPickerOpen(true)} className="btn-secondary mt-3 justify-center">
            {t('scanner.manualPick')}
          </button>
        </div>
      )}
    </article>
  )
}
