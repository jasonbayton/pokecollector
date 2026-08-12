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
 * A refusal, a missing camera, an insecure origin and an unsupported browser
 * are all standing conditions: another getUserMedia call changes nothing and,
 * for a refusal, re-prompts the user who just said no. Only a transient failure
 * is worth a retry button.
 */
export function canRetryCameraFailure(failure) {
  return failure === CAMERA_FAILURE.BUSY
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
export default function LiveCardViewfinder({ onCapture, isFull = false }) {
  const { t } = useSettings()
  const videoRef = useRef(null)
  const sessionRef = useRef(null)
  const [camera, setCamera] = useState({ status: CAMERA_STATUS.IDLE, failure: null })
  const [capturing, setCapturing] = useState(false)

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

      {/* Capped in viewport height as well as by aspect: at full modal width a
          4:3 box pushes the shutter, the staged photos and Start scanning off
          the bottom of the panel. */}
      <div className="relative mx-auto aspect-[4/3] max-h-[40vh] overflow-hidden rounded-xl border border-white/10 bg-black">
        <video
          ref={videoRef}
          muted
          playsInline
          autoPlay
          aria-label={t('scanner.liveViewfinderTitle')}
          className={`h-full w-full object-contain ${isLive ? '' : 'invisible'}`}
        />

        {isLive && (
          <div className="pointer-events-none absolute inset-0 grid place-items-center">
            <div className="aspect-[2.5/3.5] h-[86%] rounded-lg border-2 border-gold/80 shadow-[0_0_0_9999px_rgba(0,0,0,0.35)]" />
          </div>
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
                {!isStarting && !hasFailure && t('scanner.liveViewfinderHint')}
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

      {isLive ? (
        <div className="flex gap-2">
          <button
            type="button"
            onClick={handleShutter}
            disabled={capturing || isFull}
            className="btn-primary flex flex-1 items-center justify-center gap-2 py-3"
          >
            {capturing ? <Loader2 size={16} className="animate-spin" /> : <Camera size={16} />}
            <span>{capturing ? t('scanner.capturing') : t('scanner.captureCard')}</span>
          </button>
          <button
            type="button"
            onClick={handleStop}
            className="btn-ghost flex items-center justify-center gap-2"
          >
            <CameraOff size={16} />
            <span>{t('scanner.stopCamera')}</span>
          </button>
        </div>
      ) : (
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
