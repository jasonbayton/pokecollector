import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  CAMERA_FAILURE,
  CAMERA_STATUS,
  buildViewfinderConstraints,
  captureJpegFromVideo,
  classifyCameraError,
  createCameraSession,
  detectCameraSupport,
  preferContinuousAutofocus,
  stopMediaStream,
} from './cameraCapture'

function createTrack({ throwOnStop = false, unremovableListeners = false, focusModes = null } = {}) {
  const listeners = new Map()
  const track = {
    kind: 'video',
    stopCount: 0,
    stop() {
      track.stopCount += 1
      if (throwOnStop) throw new Error('this track already ended')
    },
    addEventListener(type, handler) {
      listeners.set(type, [...(listeners.get(type) || []), handler])
    },
    // The module guards for a track without one (`removeEventListener?.()`), so
    // a track that keeps its listeners is a shape it has to survive.
    removeEventListener: unremovableListeners ? undefined : (type, handler) => {
      listeners.set(type, (listeners.get(type) || []).filter(entry => entry !== handler))
    },
    emit(type) {
      for (const handler of [...(listeners.get(type) || [])]) handler()
    },
    listenerCount(type) {
      return (listeners.get(type) || []).length
    },
  }
  if (focusModes) {
    track.getCapabilities = () => ({ focusMode: focusModes })
    track.applyConstraints = vi.fn().mockResolvedValue(undefined)
  }
  return track
}

const createStream = tracks => ({ getTracks: () => tracks })

function createCanvasFactory() {
  const record = { created: [], drawImage: [], encodings: [], blobIndex: 0 }
  const createCanvas = (width, height) => {
    record.created.push({ width, height })
    const canvas = {
      width: 0,
      height: 0,
      getContext(kind) {
        record.contextKind = kind
        return { drawImage: (...args) => record.drawImage.push(args) }
      },
      toBlob(callback, type, quality) {
        record.blobIndex += 1
        record.encodings.push({ type, quality })
        callback(new Blob([`frame-${record.blobIndex}`], { type }))
      },
    }
    record.last = canvas
    return canvas
  }
  return { createCanvas, record }
}

const namedError = name => Object.assign(new Error(name), { name })

describe('buildViewfinderConstraints', () => {
  it('asks for the rear camera, 4K capture detail, and continuous focus as ideals', () => {
    expect(buildViewfinderConstraints()).toEqual({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 3840 },
        height: { ideal: 2160 },
        focusMode: { ideal: 'continuous' },
      },
    })
  })

  it('hands out a fresh object each call, so nothing downstream can poison the next request', () => {
    const first = buildViewfinderConstraints()
    const second = buildViewfinderConstraints()

    expect(first).not.toBe(second)
    expect(first.video).not.toBe(second.video)
  })
})

describe('preferContinuousAutofocus', () => {
  it('enables continuous focus when the selected video track supports it', async () => {
    const focusTrack = createTrack({ focusModes: ['manual', 'continuous'] })

    await expect(preferContinuousAutofocus(createStream([focusTrack]))).resolves.toBe(true)

    expect(focusTrack.applyConstraints).toHaveBeenCalledWith({ focusMode: 'continuous' })
  })

  it('leaves unsupported focus controls alone', async () => {
    const focusTrack = createTrack()

    await expect(preferContinuousAutofocus(createStream([focusTrack]))).resolves.toBe(false)
  })

  it('keeps scanning available when a camera rejects focus controls', async () => {
    const focusTrack = createTrack({ focusModes: ['continuous'] })
    focusTrack.applyConstraints.mockRejectedValue(new Error('unsupported by driver'))

    await expect(preferContinuousAutofocus(createStream([focusTrack]))).resolves.toBe(false)
  })
})

describe('detectCameraSupport', () => {
  it('clears a secure context that has getUserMedia', () => {
    expect(detectCameraSupport({
      isSecureContext: true,
      mediaDevices: { getUserMedia: () => {} },
    })).toBeNull()
  })

  it('blames the insecure origin, not the browser, when the origin is plain HTTP', () => {
    // A browser deletes navigator.mediaDevices on an insecure origin, so an
    // API probe first would tell an HTTP user to change browser when all they
    // need is HTTPS.
    expect(detectCameraSupport({ isSecureContext: false, mediaDevices: undefined }))
      .toBe(CAMERA_FAILURE.INSECURE)
  })

  it('reports an unsupported browser when a secure context still has no getUserMedia', () => {
    expect(detectCameraSupport({ isSecureContext: true, mediaDevices: {} }))
      .toBe(CAMERA_FAILURE.UNSUPPORTED)
    expect(detectCameraSupport(null)).toBe(CAMERA_FAILURE.UNSUPPORTED)
  })

  it('does not treat an old browser without isSecureContext as insecure', () => {
    expect(detectCameraSupport({ mediaDevices: { getUserMedia: () => {} } })).toBeNull()
  })
})

describe('classifyCameraError', () => {
  it('separates the reasons a user can act on', () => {
    expect(classifyCameraError(namedError('NotAllowedError'))).toBe(CAMERA_FAILURE.DENIED)
    expect(classifyCameraError(namedError('SecurityError'))).toBe(CAMERA_FAILURE.DENIED)
    expect(classifyCameraError(namedError('NotFoundError'))).toBe(CAMERA_FAILURE.NOT_FOUND)
    expect(classifyCameraError(namedError('OverconstrainedError'))).toBe(CAMERA_FAILURE.NOT_FOUND)
    expect(classifyCameraError(namedError('NotReadableError'))).toBe(CAMERA_FAILURE.BUSY)
    expect(classifyCameraError(namedError('AbortError'))).toBe(CAMERA_FAILURE.BUSY)
    expect(classifyCameraError(namedError('TypeError'))).toBe(CAMERA_FAILURE.UNSUPPORTED)
    expect(classifyCameraError(namedError('WhoKnowsError'))).toBe(CAMERA_FAILURE.UNKNOWN)
    expect(classifyCameraError(undefined)).toBe(CAMERA_FAILURE.UNKNOWN)
  })
})

describe('stopMediaStream', () => {
  it('stops every track', () => {
    const tracks = [createTrack(), createTrack()]

    expect(stopMediaStream(createStream(tracks))).toBe(2)
    expect(tracks.map(track => track.stopCount)).toEqual([1, 1])
  })

  it('keeps going when one track throws, so the rest are still released', () => {
    const tracks = [createTrack({ throwOnStop: true }), createTrack()]

    expect(stopMediaStream(createStream(tracks))).toBe(1)
    expect(tracks[1].stopCount).toBe(1)
  })

  it('tolerates a missing stream', () => {
    expect(stopMediaStream(null)).toBe(0)
    expect(stopMediaStream({})).toBe(0)
  })
})

describe('captureJpegFromVideo', () => {
  it('captures at the stream resolution, not at the element box, and returns a JPEG File', async () => {
    const { createCanvas, record } = createCanvasFactory()
    const video = { videoWidth: 1440, videoHeight: 1080, clientWidth: 320, clientHeight: 240 }

    const file = await captureJpegFromVideo(video, { createCanvas, now: () => 1_700_000_000_000 })

    expect(record.created).toEqual([{ width: 1440, height: 1080 }])
    expect(record.last.width).toBe(1440)
    expect(record.last.height).toBe(1080)
    expect(record.contextKind).toBe('2d')
    expect(record.drawImage).toEqual([[video, 0, 0, 1440, 1080]])
    expect(record.encodings).toEqual([{ type: 'image/jpeg', quality: 0.92 }])
    expect(file).toBeInstanceOf(File)
    expect(file.type).toBe('image/jpeg')
    expect(file.name.endsWith('.jpg')).toBe(true)
    expect(file.size).toBeGreaterThan(0)
  })

  it('refuses a frameless video rather than encoding a blank card', async () => {
    const { createCanvas, record } = createCanvasFactory()

    await expect(captureJpegFromVideo({ videoWidth: 0, videoHeight: 0 }, { createCanvas }))
      .rejects.toMatchObject({ reason: CAMERA_FAILURE.CAPTURE_FAILED })
    expect(record.created).toEqual([])
  })

  it('reports a capture failure when the canvas produces no blob', async () => {
    const createCanvas = () => ({
      getContext: () => ({ drawImage: () => {} }),
      toBlob: callback => callback(null),
    })

    await expect(captureJpegFromVideo({ videoWidth: 10, videoHeight: 10 }, { createCanvas }))
      .rejects.toMatchObject({ reason: CAMERA_FAILURE.CAPTURE_FAILED })
  })
})

describe('createCameraSession', () => {
  let track
  let stream
  let env
  let canvasFactory
  let changes

  beforeEach(() => {
    track = createTrack()
    stream = createStream([track])
    env = {
      isSecureContext: true,
      mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(stream) },
    }
    canvasFactory = createCanvasFactory()
    changes = []
  })

  const newSession = (overrides = {}) => createCameraSession({
    env,
    createCanvas: canvasFactory.createCanvas,
    now: () => 1_700_000_000_000,
    onChange: next => changes.push(next),
    ...overrides,
  })

  it('opens the rear camera on an explicit start and reports itself live', async () => {
    const session = newSession()

    const state = await session.start()

    expect(env.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1)
    expect(env.mediaDevices.getUserMedia).toHaveBeenCalledWith({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 3840 },
        height: { ideal: 2160 },
        focusMode: { ideal: 'continuous' },
      },
    })
    expect(state.status).toBe(CAMERA_STATUS.LIVE)
    expect(state.stream).toBe(stream)
    expect(changes.map(entry => entry.status)).toEqual([CAMERA_STATUS.STARTING, CAMERA_STATUS.LIVE])
  })

  it('never touches the camera until start is called', () => {
    newSession()

    expect(env.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })

  it('stages two distinct JPEGs from two shutter taps and leaves the stream running', async () => {
    const session = newSession()
    await session.start()
    const video = { videoWidth: 1280, videoHeight: 960 }

    const first = await session.capture(video)
    const second = await session.capture(video)

    expect(first.name).not.toBe(second.name)
    expect(await first.text()).not.toBe(await second.text())
    expect(canvasFactory.record.created).toHaveLength(2)
    // The stream surviving the capture is the whole feature: a stopped track
    // would force the user back through a permission-or-picker round trip.
    expect(track.stopCount).toBe(0)
    expect(session.getState().status).toBe(CAMERA_STATUS.LIVE)
  })

  it('refuses to capture when the session is not live', async () => {
    const session = newSession()

    await expect(session.capture({ videoWidth: 10, videoHeight: 10 }))
      .rejects.toMatchObject({ reason: CAMERA_FAILURE.INTERRUPTED })
  })

  it('asks again after a refusal, because the user can grant it in between', async () => {
    // The refusal message tells the user to allow the camera in their browser
    // settings. If the session then refuses to call getUserMedia again, acting
    // on that instruction does nothing and the only way back in is closing the
    // whole scanner. start() is only reached from the button, so a second call
    // is a deliberate gesture, and a browser still blocking rejects it
    // instantly without re-prompting.
    env.mediaDevices.getUserMedia.mockRejectedValueOnce(namedError('NotAllowedError'))
    const session = newSession()

    const first = await session.start()
    expect(first.failure).toBe(CAMERA_FAILURE.DENIED)
    expect(session.isDenied()).toBe(true)

    const second = await session.start()

    expect(env.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
    expect(second.status).toBe(CAMERA_STATUS.LIVE)
    expect(second.failure).toBe(null)
  })

  it('reports the refusal again when the user has not actually granted it', async () => {
    env.mediaDevices.getUserMedia.mockRejectedValue(namedError('NotAllowedError'))
    const session = newSession()

    const first = await session.start()
    const second = await session.start()

    expect(first.failure).toBe(CAMERA_FAILURE.DENIED)
    expect(second.failure).toBe(CAMERA_FAILURE.DENIED)
    // The call count is the point. Asserting only that both say denied also
    // passes under the old veto, which answered DENIED without asking at all,
    // so this test used to survive the exact regression it sits beside.
    expect(env.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  })

  it('does keep retrying a busy camera, because that one clears on its own', async () => {
    env.mediaDevices.getUserMedia.mockRejectedValueOnce(namedError('NotReadableError'))
    const session = newSession()

    const first = await session.start()
    const second = await session.start()

    expect(first.failure).toBe(CAMERA_FAILURE.BUSY)
    expect(second.status).toBe(CAMERA_STATUS.LIVE)
    expect(env.mediaDevices.getUserMedia).toHaveBeenCalledTimes(2)
  })

  it('reports an insecure origin without calling getUserMedia at all', async () => {
    env.isSecureContext = false
    const session = newSession()

    const state = await session.start()

    expect(state.failure).toBe(CAMERA_FAILURE.INSECURE)
    expect(env.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })

  it('records a blocked environment on probe, without touching the camera', () => {
    env.isSecureContext = false
    const session = newSession()

    const state = session.probeSupport()

    expect(state.status).toBe(CAMERA_STATUS.ERROR)
    expect(state.failure).toBe(CAMERA_FAILURE.INSECURE)
    expect(env.mediaDevices.getUserMedia).not.toHaveBeenCalled()
    expect(changes.map(entry => entry.status)).toEqual([CAMERA_STATUS.ERROR])
  })

  it('says nothing on probe when the environment can open a camera', () => {
    const session = newSession()

    expect(session.probeSupport().status).toBe(CAMERA_STATUS.IDLE)
    expect(changes).toEqual([])
    expect(env.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })

  it('stops every track on stop', async () => {
    const second = createTrack()
    env.mediaDevices.getUserMedia.mockResolvedValue(createStream([track, second]))
    const session = newSession()
    await session.start()

    const state = session.stop()

    expect(track.stopCount).toBe(1)
    expect(second.stopCount).toBe(1)
    expect(state.status).toBe(CAMERA_STATUS.IDLE)
    expect(state.stream).toBeNull()
  })

  it('stops the old stream when a start replaces it', async () => {
    const replacementTrack = createTrack()
    env.mediaDevices.getUserMedia
      .mockResolvedValueOnce(stream)
      .mockResolvedValueOnce(createStream([replacementTrack]))
    const session = newSession()
    await session.start()

    await session.start()

    expect(track.stopCount).toBe(1)
    expect(replacementTrack.stopCount).toBe(0)
    expect(session.getState().status).toBe(CAMERA_STATUS.LIVE)
  })

  it('stops a stream that arrives after the session was already stopped', async () => {
    let releaseStream
    env.mediaDevices.getUserMedia.mockImplementation(
      () => new Promise(resolve => { releaseStream = resolve }),
    )
    const session = newSession()
    const starting = session.start()

    session.stop()
    releaseStream(stream)
    await starting

    // Nothing will ever attach this stream. Without the generation check its
    // track keeps the camera light on until the tab is closed.
    expect(track.stopCount).toBe(1)
    expect(session.getState().status).toBe(CAMERA_STATUS.IDLE)
  })

  it('stops a stream that arrives after the owner unmounted', async () => {
    let releaseStream
    env.mediaDevices.getUserMedia.mockImplementation(
      () => new Promise(resolve => { releaseStream = resolve }),
    )
    const session = newSession()
    const starting = session.start()

    session.dispose()
    releaseStream(stream)
    await starting

    expect(track.stopCount).toBe(1)
  })

  it('treats a track ending as an interruption and releases the rest of the stream', async () => {
    const audioLikeSecondTrack = createTrack()
    env.mediaDevices.getUserMedia.mockResolvedValue(createStream([track, audioLikeSecondTrack]))
    const session = newSession()
    await session.start()
    changes.length = 0

    track.emit('ended')

    expect(changes).toHaveLength(1)
    expect(changes[0]).toMatchObject({
      status: CAMERA_STATUS.ERROR,
      failure: CAMERA_FAILURE.INTERRUPTED,
      stream: null,
    })
    expect(audioLikeSecondTrack.stopCount).toBe(1)
  })

  it('unhooks the ended listener from the stream a start replaces', async () => {
    env.mediaDevices.getUserMedia
      .mockResolvedValueOnce(stream)
      .mockResolvedValueOnce(createStream([createTrack()]))
    const session = newSession()
    await session.start()
    expect(track.listenerCount('ended')).toBe(1)

    await session.start()
    changes.length = 0

    // The replaced stream is stopped and unhooked, so its ended event never
    // reaches the session in the first place.
    expect(track.listenerCount('ended')).toBe(0)
    track.emit('ended')
    expect(changes).toEqual([])
    expect(session.getState().status).toBe(CAMERA_STATUS.LIVE)
  })

  it('ignores an ended track from a replaced stream that could not be unhooked', async () => {
    // A track that keeps its listeners outlives the unhook, so a stale ended
    // event does still arrive. Without the identity check it would tear down
    // the camera that is currently live and report an interruption that never
    // happened - the user loses the viewfinder mid-batch for nothing.
    const stubborn = createTrack({ unremovableListeners: true })
    env.mediaDevices.getUserMedia
      .mockResolvedValueOnce(createStream([stubborn]))
      .mockResolvedValueOnce(stream)
    const session = newSession()
    await session.start()
    await session.start()
    changes.length = 0
    expect(stubborn.listenerCount('ended')).toBe(1)

    stubborn.emit('ended')

    expect(changes).toEqual([])
    expect(session.getState().status).toBe(CAMERA_STATUS.LIVE)
    expect(session.getState().stream).toBe(stream)
    expect(track.stopCount).toBe(0)
  })

  it('stops the camera when the document is hidden, and unbinds cleanly', async () => {
    const listeners = new Map()
    const doc = {
      hidden: false,
      addEventListener: (type, handler) => listeners.set(type, handler),
      removeEventListener: type => listeners.delete(type),
    }
    const session = newSession()
    const unbind = session.bindVisibility(doc)
    await session.start()

    doc.hidden = true
    listeners.get('visibilitychange')()

    expect(track.stopCount).toBe(1)
    expect(session.getState().status).toBe(CAMERA_STATUS.IDLE)

    unbind()
    expect(listeners.has('visibilitychange')).toBe(false)
  })

  it('keeps a standing failure when the tab is hidden, instead of offering a start that cannot work', async () => {
    env.mediaDevices.getUserMedia.mockRejectedValue(namedError('NotAllowedError'))
    const listeners = new Map()
    const doc = {
      hidden: false,
      addEventListener: (type, handler) => listeners.set(type, handler),
      removeEventListener: type => listeners.delete(type),
    }
    const session = newSession()
    session.bindVisibility(doc)
    await session.start()
    expect(session.getState().failure).toBe(CAMERA_FAILURE.DENIED)

    doc.hidden = true
    listeners.get('visibilitychange')()

    // Hiding a tab releases the stream; it does not un-refuse a refusal. Going
    // idle here would swap the explanation for the neutral hint and put back a
    // Start button whose only possible outcome is the same refusal again.
    expect(session.getState().status).toBe(CAMERA_STATUS.ERROR)
    expect(session.getState().failure).toBe(CAMERA_FAILURE.DENIED)
  })

  it('leaves the camera alone when the document becomes visible again', async () => {
    const listeners = new Map()
    const doc = {
      hidden: false,
      addEventListener: (type, handler) => listeners.set(type, handler),
      removeEventListener: type => listeners.delete(type),
    }
    const session = newSession()
    session.bindVisibility(doc)
    await session.start()

    listeners.get('visibilitychange')()

    expect(track.stopCount).toBe(0)
    expect(session.getState().status).toBe(CAMERA_STATUS.LIVE)
  })

  it('stops every track on dispose and drops the ended listeners', async () => {
    const session = newSession()
    await session.start()
    expect(track.listenerCount('ended')).toBe(1)

    session.dispose()

    expect(track.stopCount).toBe(1)
    expect(track.listenerCount('ended')).toBe(0)
    expect(session.getState().status).toBe(CAMERA_STATUS.IDLE)
  })

  it('throws away a frame whose session closed while it was still encoding', async () => {
    // Encoding is asynchronous. Without a generation check the shutter tap that
    // was in flight when the user closed the scanner still resolves, and its
    // file is staged into a scanner that is no longer open - a photo the user
    // never knowingly took, appearing on the next open.
    let release
    const session = newSession({
      createCanvas: () => ({
        width: 0,
        height: 0,
        getContext: () => ({ drawImage: () => {} }),
        toBlob: (callback) => { release = () => callback(new Blob(['late'], { type: 'image/jpeg' })) },
      }),
    })
    await session.start()

    const pending = session.capture({ videoWidth: 1280, videoHeight: 720 })
    session.dispose()
    release()

    await expect(pending).rejects.toMatchObject({ reason: CAMERA_FAILURE.INTERRUPTED })
  })

  it('throws away a frame whose session was stopped while it was still encoding', async () => {
    let release
    const session = newSession({
      createCanvas: () => ({
        width: 0,
        height: 0,
        getContext: () => ({ drawImage: () => {} }),
        toBlob: (callback) => { release = () => callback(new Blob(['late'], { type: 'image/jpeg' })) },
      }),
    })
    await session.start()

    const pending = session.capture({ videoWidth: 1280, videoHeight: 720 })
    session.stop()
    release()

    await expect(pending).rejects.toMatchObject({ reason: CAMERA_FAILURE.INTERRUPTED })
  })

  it('will not reopen the camera after dispose', async () => {
    const session = newSession()
    session.dispose()

    await session.start()

    expect(env.mediaDevices.getUserMedia).not.toHaveBeenCalled()
  })
})
