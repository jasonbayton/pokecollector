/**
 * Camera plumbing for the live viewfinder.
 *
 * Everything that touches getUserMedia, MediaStreamTrack or a canvas lives here
 * rather than in the React component, so the parts that cannot run in this
 * repository's DOM-less test environment are reachable through injected
 * collaborators instead of a browser.
 *
 * The rule this module exists to enforce: a MediaStreamTrack that is never
 * stopped leaves the camera light on. Every path that abandons a stream -
 * replacement, close, hidden tab, an unmount that races an open permission
 * prompt - has to stop its tracks.
 *
 * Its corollary: releasing a stream is not the same as clearing a failure. A
 * refusal, a missing camera, an insecure origin and an unsupported browser
 * outlive the stream, so the session keeps reporting them until something
 * actually changes.
 */

export const CAMERA_FAILURE = Object.freeze({
  UNSUPPORTED: 'unsupported',
  INSECURE: 'insecure',
  DENIED: 'denied',
  NOT_FOUND: 'notFound',
  BUSY: 'busy',
  INTERRUPTED: 'interrupted',
  CAPTURE_FAILED: 'captureFailed',
  UNKNOWN: 'unknown',
})

export const CAMERA_STATUS = Object.freeze({
  IDLE: 'idle',
  STARTING: 'starting',
  LIVE: 'live',
  ERROR: 'error',
})

export class CameraError extends Error {
  constructor(reason, message) {
    super(message || reason)
    this.name = 'CameraError'
    this.reason = reason
  }
}

/**
 * A fresh constraints object per call. The rear camera is requested with
 * `ideal` rather than `exact` so a laptop with only a front camera still gets a
 * viewfinder instead of an OverconstrainedError.
 */
export function buildViewfinderConstraints() {
  return {
    audio: false,
    video: {
      facingMode: { ideal: 'environment' },
      width: { ideal: 1920 },
      height: { ideal: 1080 },
    },
  }
}

function defaultEnv() {
  if (typeof globalThis.navigator === 'undefined') return null
  return {
    isSecureContext: globalThis.isSecureContext,
    mediaDevices: globalThis.navigator.mediaDevices,
  }
}

/**
 * Returns a CAMERA_FAILURE reason when a live viewfinder cannot be opened at
 * all, or null when it is worth trying.
 *
 * The secure-context check comes first on purpose: a browser removes
 * navigator.mediaDevices entirely on an insecure origin, so probing the API
 * first would tell a plain-HTTP user their browser is too old when all they
 * need is HTTPS.
 */
export function detectCameraSupport(env = defaultEnv()) {
  if (!env) return CAMERA_FAILURE.UNSUPPORTED
  if (env.isSecureContext === false) return CAMERA_FAILURE.INSECURE
  if (typeof env.mediaDevices?.getUserMedia !== 'function') return CAMERA_FAILURE.UNSUPPORTED
  return null
}

export function classifyCameraError(error) {
  switch (error?.name) {
    case 'NotAllowedError':
    case 'PermissionDeniedError':
    case 'SecurityError':
      return CAMERA_FAILURE.DENIED
    case 'NotFoundError':
    case 'DevicesNotFoundError':
    case 'OverconstrainedError':
      return CAMERA_FAILURE.NOT_FOUND
    case 'NotReadableError':
    case 'TrackStartError':
    case 'AbortError':
      return CAMERA_FAILURE.BUSY
    case 'TypeError':
      return CAMERA_FAILURE.UNSUPPORTED
    default:
      return CAMERA_FAILURE.UNKNOWN
  }
}

/**
 * Stops every track on a stream and reports how many it stopped. A track that
 * throws on stop (already ended by the browser) must not prevent the rest of
 * the stream being released.
 */
export function stopMediaStream(stream) {
  const tracks = typeof stream?.getTracks === 'function' ? stream.getTracks() : []
  let stopped = 0
  for (const track of tracks) {
    try {
      track.stop()
      stopped += 1
    } catch {
      // The browser ended this track already; the rest still need stopping.
    }
  }
  return stopped
}

function defaultCreateCanvas(width, height) {
  if (typeof document === 'undefined') {
    throw new CameraError(CAMERA_FAILURE.CAPTURE_FAILED, 'No document to draw the frame into.')
  }
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  return canvas
}

function canvasToJpegBlob(canvas, quality) {
  if (typeof canvas.toBlob === 'function') {
    return new Promise(resolve => { canvas.toBlob(resolve, 'image/jpeg', quality) })
  }
  if (typeof canvas.convertToBlob === 'function') {
    return canvas.convertToBlob({ type: 'image/jpeg', quality })
  }
  return Promise.resolve(null)
}

// Two captures inside the same millisecond would otherwise share a filename,
// and the staged list would look like one photo added twice.
let captureSequence = 0

/**
 * Draws the current frame at the stream's own resolution and returns it as a
 * JPEG File, ready to stage exactly like a file the user picked.
 *
 * videoWidth/videoHeight are the intrinsic frame size. The element's CSS box is
 * deliberately not consulted: a viewfinder is letterboxed to fit the panel, and
 * capturing the box would throw away most of the sensor's pixels.
 */
export async function captureJpegFromVideo(video, {
  createCanvas = defaultCreateCanvas,
  quality = 0.92,
  now = Date.now,
} = {}) {
  const width = Math.round(video?.videoWidth || 0)
  const height = Math.round(video?.videoHeight || 0)
  if (!width || !height) {
    // getUserMedia resolves before the video element necessarily receives
    // loadedmetadata. The session is still live, but there is no frame yet.
    // Keep this as a frame failure so the live shutter remains a useful retry.
    throw new CameraError(CAMERA_FAILURE.CAPTURE_FAILED, 'The viewfinder has no frame to capture.')
  }

  const canvas = createCanvas(width, height)
  canvas.width = width
  canvas.height = height
  const context = typeof canvas.getContext === 'function' ? canvas.getContext('2d') : null
  if (!context) throw new CameraError(CAMERA_FAILURE.CAPTURE_FAILED, 'No 2d context for the capture canvas.')
  context.drawImage(video, 0, 0, width, height)

  const blob = await canvasToJpegBlob(canvas, quality)
  if (!blob) throw new CameraError(CAMERA_FAILURE.CAPTURE_FAILED, 'The frame could not be encoded as JPEG.')

  captureSequence += 1
  const stamp = new Date(now()).toISOString().replace(/[:.]/g, '-')
  return new File([blob], `card-${stamp}-${String(captureSequence).padStart(4, '0')}.jpg`, {
    type: 'image/jpeg',
    lastModified: now(),
  })
}

/**
 * A camera session owns exactly one stream at a time and is the only thing
 * allowed to hold one.
 *
 * `onChange` is called with a plain snapshot on every transition so a React
 * component can mirror it into state without reaching for the stream itself.
 */
export function createCameraSession({
  env,
  createCanvas = defaultCreateCanvas,
  now = Date.now,
  onChange = () => {},
} = {}) {
  const resolveEnv = () => env ?? defaultEnv()

  let state = { status: CAMERA_STATUS.IDLE, failure: null, stream: null }
  let deniedOnce = false
  let disposed = false
  // Bumped by stop(), dispose() and every start(). An awaited getUserMedia that
  // comes back holding a stale token has been abandoned mid-prompt.
  let generation = 0
  let untrackEnded = null

  const getState = () => ({ ...state })

  const publish = next => {
    state = next
    onChange(getState())
  }

  const releaseStream = () => {
    if (untrackEnded) {
      untrackEnded()
      untrackEnded = null
    }
    if (state.stream) stopMediaStream(state.stream)
  }

  const watchForInterruption = stream => {
    const tracks = typeof stream.getTracks === 'function' ? stream.getTracks() : []
    const handleEnded = () => {
      // A track from a stream we already replaced or released is not news.
      if (state.stream !== stream) return
      releaseStream()
      publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.INTERRUPTED, stream: null })
    }
    tracks.forEach(track => track.addEventListener?.('ended', handleEnded))
    return () => tracks.forEach(track => track.removeEventListener?.('ended', handleEnded))
  }

  async function start() {
    if (disposed) return getState()

    if (deniedOnce) {
      // Asking a second time after a refusal either re-prompts or silently
      // rejects, depending on the browser. Neither helps, and the first is the
      // nag the user just said no to. Report the standing denial instead.
      publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.DENIED, stream: null })
      return getState()
    }

    const blocked = detectCameraSupport(resolveEnv())
    if (blocked) {
      publish({ status: CAMERA_STATUS.ERROR, failure: blocked, stream: null })
      return getState()
    }

    generation += 1
    const token = generation
    releaseStream()
    publish({ status: CAMERA_STATUS.STARTING, failure: null, stream: null })

    let stream
    try {
      stream = await resolveEnv().mediaDevices.getUserMedia(buildViewfinderConstraints())
    } catch (error) {
      if (token !== generation || disposed) return getState()
      const failure = classifyCameraError(error)
      if (failure === CAMERA_FAILURE.DENIED) deniedOnce = true
      publish({ status: CAMERA_STATUS.ERROR, failure, stream: null })
      return getState()
    }

    if (token !== generation || disposed) {
      // stop(), dispose() or a replacement start() won the race while the
      // permission prompt was open. Nobody will ever attach this stream, so
      // without this its tracks keep the camera light on for good.
      stopMediaStream(stream)
      return getState()
    }

    untrackEnded = watchForInterruption(stream)
    publish({ status: CAMERA_STATUS.LIVE, failure: null, stream })
    return getState()
  }

  /**
   * Records an environment that cannot open a camera at all, without touching
   * the camera. The owner calls this on mount so a standing block lives in the
   * session rather than in its own state, where the next stop() would publish
   * over it.
   */
  function probeSupport() {
    if (disposed) return getState()
    const blocked = detectCameraSupport(resolveEnv())
    if (blocked) publish({ status: CAMERA_STATUS.ERROR, failure: blocked, stream: null })
    return getState()
  }

  function stop() {
    generation += 1
    releaseStream()
    if (state.status === CAMERA_STATUS.ERROR) {
      // Releasing the stream because the tab went away changes nothing about a
      // refusal, a missing camera, an insecure origin or an unsupported
      // browser. Going idle here would swap the explanation for the neutral
      // hint and put back a Start button whose only possible outcome is the
      // same failure again.
      publish({ status: CAMERA_STATUS.ERROR, failure: state.failure, stream: null })
      return getState()
    }
    publish({ status: CAMERA_STATUS.IDLE, failure: null, stream: null })
    return getState()
  }

  async function capture(video) {
    if (state.status !== CAMERA_STATUS.LIVE) {
      throw new CameraError(CAMERA_FAILURE.INTERRUPTED, 'The viewfinder is not live.')
    }
    // Deliberately leaves the stream running: the whole point of the viewfinder
    // is that the next card can be captured without reopening anything.
    return captureJpegFromVideo(video, { createCanvas, now })
  }

  function bindVisibility(target) {
    if (typeof target?.addEventListener !== 'function') return () => {}
    const handleVisibility = () => {
      if (target.hidden) stop()
    }
    target.addEventListener('visibilitychange', handleVisibility)
    return () => target.removeEventListener('visibilitychange', handleVisibility)
  }

  function dispose() {
    // No publish: the owner is unmounting and has nowhere to put the update.
    disposed = true
    generation += 1
    releaseStream()
    state = { status: CAMERA_STATUS.IDLE, failure: null, stream: null }
  }

  return {
    bindVisibility,
    capture,
    dispose,
    getState,
    isDenied: () => deniedOnce,
    probeSupport,
    start,
    stop,
  }
}
