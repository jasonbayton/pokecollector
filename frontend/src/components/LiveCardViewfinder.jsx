import { useEffect, useRef, useState } from 'react'
import { Camera, CameraOff, Loader2, RefreshCw, Video } from 'lucide-react'

import { useSettings } from '../contexts/SettingsContext'
import {
  CAMERA_FAILURE,
  CAMERA_STATUS,
  createCameraSession,
} from '../utils/cameraCapture'

/**
 * Every branch calls t() with a literal key so the translation check can see
 * them. Building the key from the failure name would hide all eight from it.
 *
 * That check only reports keys the source asks for and en.js lacks, so it
 * cannot see a branch pointed at the wrong sentence: the mapping itself is
 * pinned in LiveCardViewfinder.test.js instead.
 */
export function cameraFailureMessage(t, failure) {
  switch (failure) {
    case CAMERA_FAILURE.INSECURE:
      return t('scanner.cameraErrorInsecure')
    case CAMERA_FAILURE.UNSUPPORTED:
      return t('scanner.cameraErrorUnsupported')
    case CAMERA_FAILURE.DENIED:
      return t('scanner.cameraErrorDenied')
    case CAMERA_FAILURE.NOT_FOUND:
      return t('scanner.cameraErrorNotFound')
    case CAMERA_FAILURE.BUSY:
      return t('scanner.cameraErrorBusy')
    case CAMERA_FAILURE.INTERRUPTED:
      return t('scanner.cameraErrorInterrupted')
    case CAMERA_FAILURE.CAPTURE_FAILED:
      return t('scanner.cameraErrorCaptureFailed')
    default:
      return t('scanner.cameraErrorUnknown')
  }
}

/**
 * A missing camera, an insecure origin and an unsupported browser are standing
 * conditions that another getUserMedia call cannot change, so they get no retry.
 *
 * A refusal is different, and used not to be offered one. The message tells the
 * user to allow the camera in their browser settings; without a retry the only
 * way to act on that was to close the whole scanner and reopen it. Re-prompting
 * is not a risk: a browser that is still blocking rejects immediately and
 * silently, so the worst case is the same message again.
 */
/**
 * Which axis the card alignment guide is bound by.
 *
 * A frame wider than a card has spare width, so the guide is sized from the
 * height. A frame NARROWER than a card is the other way round, and sizing from
 * the height there runs the guide's side borders straight off the picture.
 * That is what happened once the frame started matching a portrait phone
 * stream instead of a fixed 4:3 box.
 *
 * The 4:3 fallback applies before the browser reports the stream, and is wider
 * than a card, so it stays height-bound exactly as the old fixed box was.
 */
export function guideIsHeightBound(streamSize) {
  const ratio = streamSize && streamSize.height > 0
    ? streamSize.width / streamSize.height
    : 4 / 3
  return ratio >= 2.5 / 3.5
}

export function canRetryCameraFailure(failure) {
  return failure === CAMERA_FAILURE.BUSY
    || failure === CAMERA_FAILURE.DENIED
    || failure === CAMERA_FAILURE.INTERRUPTED
    || failure === CAMERA_FAILURE.CAPTURE_FAILED
    || failure === CAMERA_FAILURE.UNKNOWN
}

/**
 * A live camera preview with a shutter that stages one JPEG per tap.
 *
 * The OS camera app behind <input capture> forces a confirm-and-return step per
 * card, and it cannot be reopened programmatically because showing a file
 * picker needs transient activation. This is the only route to a rapid batch.
 * It is an addition, never a replacement: the file inputs it sits beside stay
 * as the fallback for every device and permission state this cannot serve.
 */
export default function LiveCardViewfinder({ onCapture, isFull = false, singleShot = false, onClose }) {
  const { t } = useSettings()
  const videoRef = useRef(null)
  const sessionRef = useRef(null)
  const [camera, setCamera] = useState({ status: CAMERA_STATUS.IDLE, failure: null })
  const [capturing, setCapturing] = useState(false)
  // The stream's own aspect ratio, once the browser reports it. A hardcoded
  // box cannot be right for both: a phone hands back a portrait stream and a
  // desktop webcam a landscape one, so any fixed ratio pillarboxes or
  // letterboxes one of them. object-contain then shrinks the card to fit the
  // wrong axis, which is why the card was using about 40% of the frame width
  // on a phone.
  const [streamSize, setStreamSize] = useState(null)

  const getSession = () => {
    if (!sessionRef.current) {
      sessionRef.current = createCameraSession({
        onChange: next => setCamera({ status: next.status, failure: next.failure }),
      })
    }
    return sessionRef.current
  }

  useEffect(() => {
    // Creating the session does not touch the camera; only start() does, and
    // start() is only ever reached from the button's own click handler.
    const session = getSession()
    const unbindVisibility = session.bindVisibility(typeof document === 'undefined' ? null : document)
    // The session records the block rather than this component, so that hiding
    // the tab later cannot publish an idle state over the explanation.
    session.probeSupport()

    return () => {
      unbindVisibility()
      if (videoRef.current) videoRef.current.srcObject = null
      session.dispose()
      // Drop the ref as well as disposing. A disposed session refuses start()
      // silently, and sessionRef outlives this cleanup, so keeping it would
      // hand the next mount a session that can never open the camera again.
      // StrictMode makes that the very first thing that happens in
      // development, but any remount does it in production too.
      if (sessionRef.current === session) sessionRef.current = null
    }
  }, [])

  // Attaching the stream from the status transition rather than from inside the
  // click handler keeps the element and the session in step no matter which one
  // ended the stream.
  useEffect(() => {
    const video = videoRef.current
    if (!video) return
    const stream = camera.status === CAMERA_STATUS.LIVE
      ? (sessionRef.current?.getState().stream ?? null)
      : null
    if (video.srcObject !== stream) video.srcObject = stream
    if (!stream) return
    const played = video.play?.()
    // An autoplay rejection is not fatal; muted + playsInline is the combination
    // browsers allow, and the frames are still there to capture.
    if (played && typeof played.catch === 'function') played.catch(() => {})
  }, [camera.status])

  const handleStart = () => getSession().start()

  const handleStop = () => getSession().stop()

  const handleShutter = async () => {
    if (capturing || isFull || camera.status !== CAMERA_STATUS.LIVE) return
    setCapturing(true)
    try {
      const file = await getSession().capture(videoRef.current)
      // A tap that worked clears the last tap's message; leaving it up says the
      // capture the user just took failed. Read the session rather than
      // blanking the failure outright: the stream can die while the frame is
      // being drawn, and that one has to survive this line.
      const settled = getSession().getState()
      setCamera({ status: settled.status, failure: settled.failure })
      onCapture?.(file)
      if (singleShot) onClose?.()
    } catch (error) {
      // The session's own status, not a hardcoded live: a frame can fail
      // because the stream died under us, and that leaves the session in error
      // with a retry to offer.
      setCamera({
        status: getSession().getState().status,
        failure: error?.reason || CAMERA_FAILURE.CAPTURE_FAILED,
      })
    } finally {
      setCapturing(false)
    }
  }

  const guideFitsByHeight = guideIsHeightBound(streamSize)

  const isLive = camera.status === CAMERA_STATUS.LIVE
  const isStarting = camera.status === CAMERA_STATUS.STARTING
  const hasFailure = Boolean(camera.failure)

  return (
    <div className="space-y-3 rounded-2xl border border-border bg-bg-surface p-3">
      <div className="flex items-center justify-between gap-3">
        <p className="text-[10px] font-black uppercase tracking-[0.2em] text-text-muted">
          {t('scanner.liveViewfinderTitle')}
        </p>
        {isLive && (
          <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-wider text-brand-red">
            <span className="h-2 w-2 rounded-full bg-brand-red" aria-hidden />
            {t('scanner.cameraLive')}
          </span>
        )}
      </div>

      {/* Sized to the stream once the browser reports it, so the picture fills
          the frame instead of sitting in bars. 4:3 only until then, and still
          capped in viewport height: at full modal width a tall portrait stream
          would otherwise push the shutter and Start scanning off the bottom of
          the panel. The cap is the reason this is max-h rather than a height. */}
      <div
        style={streamSize ? { aspectRatio: `${streamSize.width} / ${streamSize.height}` } : undefined}
        className={`relative mx-auto max-h-[62dvh] overflow-hidden rounded-xl border border-white/10 bg-black ${streamSize ? '' : 'aspect-[4/3]'}`}
      >
        <video
          ref={videoRef}
          muted
          playsInline
          autoPlay
          onLoadedMetadata={event => {
            const { videoWidth, videoHeight } = event.currentTarget
            if (videoWidth > 0 && videoHeight > 0) setStreamSize({ width: videoWidth, height: videoHeight })
          }}
          aria-label={t('scanner.liveViewfinderTitle')}
          className={`h-full w-full object-contain ${isLive ? '' : 'invisible'}`}
        />

        {isLive && (
          <>
            <div className="pointer-events-none absolute inset-0 grid place-items-center">
              <div className={`aspect-[2.5/3.5] rounded-lg border-2 border-gold/80 shadow-[0_0_0_9999px_rgba(0,0,0,0.35)] ${guideFitsByHeight ? 'h-[86%]' : 'w-[86%]'}`} />
            </div>
            <div className="absolute inset-x-0 bottom-0 flex gap-2 bg-gradient-to-t from-black/80 to-transparent p-3 pt-8">
              <button
                type="button"
                onClick={handleShutter}
                disabled={capturing || isFull}
                className="btn-primary flex flex-1 items-center justify-center gap-2 py-3"
              >
                {capturing ? <Loader2 size={16} className="animate-spin" /> : <Camera size={16} />}
                <span>{capturing ? t('scanner.capturing') : t(singleShot ? 'scanner.replacePhoto' : 'scanner.captureCard')}</span>
              </button>
              <button
                type="button"
                onClick={handleStop}
                aria-label={t('scanner.stopCamera')}
                className="btn-ghost flex items-center justify-center gap-2 bg-black/50 px-4"
              >
                <CameraOff size={16} />
              </button>
            </div>
          </>
        )}

        {!isLive && (
          <div className="absolute inset-0 grid place-items-center p-4 text-center">
            <div className="space-y-2">
              {isStarting
                ? <Loader2 size={22} className="mx-auto animate-spin text-text-muted" aria-hidden />
                : <CameraOff size={22} className="mx-auto text-text-muted" aria-hidden />}
              <p className="text-xs leading-relaxed text-text-secondary" role="status">
                {isStarting && t('scanner.startingCamera')}
                {!isStarting && hasFailure && cameraFailureMessage(t, camera.failure)}
                {!isStarting && !hasFailure && t(singleShot ? 'scanner.liveViewfinderHintSingle' : 'scanner.liveViewfinderHint')}
              </p>
              {!isStarting && hasFailure && (
                <p className="text-[11px] text-text-muted">{t('scanner.cameraFallbackHint')}</p>
              )}
            </div>
          </div>
        )}
      </div>

      {isLive && camera.failure && (
        <p className="text-xs text-brand-red" role="status">
          {cameraFailureMessage(t, camera.failure)}
        </p>
      )}

      {isLive ? null : (
        (!hasFailure || canRetryCameraFailure(camera.failure)) && (
          <button
            type="button"
            onClick={handleStart}
            disabled={isStarting}
            className="btn-secondary flex w-full items-center justify-center gap-2"
          >
            {isStarting && <Loader2 size={16} className="animate-spin" />}
            {!isStarting && hasFailure && <RefreshCw size={16} />}
            {!isStarting && !hasFailure && <Video size={16} />}
            <span>
              {isStarting && t('scanner.startingCamera')}
              {!isStarting && hasFailure && t('scanner.retryCamera')}
              {!isStarting && !hasFailure && t('scanner.startCamera')}
            </span>
          </button>
        )
      )}

      {isFull && isLive && (
        <p className="text-xs text-text-muted" role="status">{t('scanner.cameraBatchFull')}</p>
      )}
    </div>
  )
}
