import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { hookHarness, findAll, walkTree } from '../test/hookHarness'

// The component's own hooks are the thing under test here, so the harness owns
// them. Everything else in the react module stays real.
vi.mock('react', async importOriginal => ({
  ...(await importOriginal()),
  ...hookHarness.hooks,
}))

const camera = vi.hoisted(() => ({
  sessions: [],
  supportFailure: null,
}))

/**
 * A stand-in session, so this file can put the component in states a stubbed
 * browser cannot reach on demand. It mirrors the real session's contract, and
 * LiveCardViewfinder.integration.test.js - the same component against the
 * shipped createCameraSession - is what keeps the mirror honest.
 */
vi.mock('../utils/cameraCapture', async importOriginal => {
  const actual = await importOriginal()
  return {
    ...actual,
    createCameraSession: options => {
      const session = {
        options,
        state: { status: actual.CAMERA_STATUS.IDLE, failure: null, stream: null },
        unbindVisibility: vi.fn(),
        boundTo: null,
      }
      session.publish = next => {
        session.state = { ...session.state, ...next }
        options.onChange({ ...session.state })
      }
      session.start = vi.fn(async () => session.state)
      session.probeSupport = vi.fn(() => {
        if (camera.supportFailure) {
          session.publish({
            status: actual.CAMERA_STATUS.ERROR,
            failure: camera.supportFailure,
            stream: null,
          })
        }
        return { ...session.state }
      })
      session.stop = vi.fn(() => {
        // Stopping releases the stream; it never publishes over a standing
        // failure, so a hidden tab cannot wipe the reason the camera is off.
        if (session.state.status === actual.CAMERA_STATUS.ERROR) {
          session.publish({ stream: null })
          return { ...session.state }
        }
        session.publish({ status: actual.CAMERA_STATUS.IDLE, failure: null, stream: null })
        return { ...session.state }
      })
      session.capture = vi.fn()
      session.dispose = vi.fn()
      session.getState = vi.fn(() => ({ ...session.state }))
      session.isDenied = vi.fn(() => false)
      session.bindVisibility = vi.fn(target => {
        session.boundTo = target
        return session.unbindVisibility
      })
      camera.sessions.push(session)
      return session
    },
  }
})

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

const { CAMERA_FAILURE, CAMERA_STATUS } = await import('../utils/cameraCapture')
const viewfinderModule = await import('./LiveCardViewfinder')
const { cameraFailureMessage, canRetryCameraFailure } = viewfinderModule
const LiveCardViewfinder = viewfinderModule.default
const en = (await import('../i18n/en')).default

let props
let fakeVideo
let fakeDocument

const render = (overrides = {}) => hookHarness.renderAndFlush(LiveCardViewfinder, { ...props, ...overrides })

const session = () => camera.sessions[0]

const buttonsWithText = tree => findAll(tree, node => node.type === 'button')

const textOf = tree => [...walkTree(tree)]
  .flatMap(node => (Array.isArray(node.props?.children) ? node.props.children : [node.props?.children]))
  .filter(child => typeof child === 'string')

/** Attaches a stand-in <video> to the ref the component just handed React. */
const attachVideo = tree => {
  const video = [...walkTree(tree)].find(node => node.type === 'video')
  // React 18 lifts ref off props onto the element itself.
  const ref = video.ref ?? video.props.ref
  ref.current = fakeVideo
  return video
}

beforeEach(() => {
  hookHarness.reset()
  camera.sessions.length = 0
  camera.supportFailure = null
  props = { onCapture: vi.fn(), isFull: false }
  fakeVideo = { srcObject: undefined, play: vi.fn(() => Promise.resolve()) }
  fakeDocument = { hidden: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }
  vi.stubGlobal('document', fakeDocument)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('cameraFailureMessage', () => {
  const keyFor = failure => cameraFailureMessage(key => key, failure)
  const resolveEnglish = key => key.split('.').reduce((value, part) => value?.[part], en)

  it('gives every failure its own sentence', () => {
    expect(Object.fromEntries(
      Object.entries(CAMERA_FAILURE).map(([name, failure]) => [name, keyFor(failure)]),
    )).toEqual({
      UNSUPPORTED: 'scanner.cameraErrorUnsupported',
      INSECURE: 'scanner.cameraErrorInsecure',
      DENIED: 'scanner.cameraErrorDenied',
      NOT_FOUND: 'scanner.cameraErrorNotFound',
      BUSY: 'scanner.cameraErrorBusy',
      INTERRUPTED: 'scanner.cameraErrorInterrupted',
      CAPTURE_FAILED: 'scanner.cameraErrorCaptureFailed',
      UNKNOWN: 'scanner.cameraErrorUnknown',
    })
  })

  it('resolves each of those keys to real English, and never reuses one', () => {
    // The translation gate only catches keys the source asks for and en.js
    // lacks. Pointing two failures at one sentence passes that gate and
    // silently orphans the sentence nothing asks for any more, so the mapping
    // has to be pinned here instead.
    const keys = Object.values(CAMERA_FAILURE).map(keyFor)

    for (const key of keys) expect(typeof resolveEnglish(key)).toBe('string')
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('falls back to the unknown sentence for a reason it has never heard of', () => {
    expect(keyFor(undefined)).toBe('scanner.cameraErrorUnknown')
    expect(keyFor('someReasonAddedLater')).toBe('scanner.cameraErrorUnknown')
  })
})

describe('canRetryCameraFailure', () => {
  it('offers a retry for exactly the failures that can clear on their own', () => {
    expect(Object.fromEntries(
      Object.entries(CAMERA_FAILURE).map(([name, failure]) => [name, canRetryCameraFailure(failure)]),
    )).toEqual({
      // Standing conditions: another getUserMedia call cannot change any of
      // these.
      UNSUPPORTED: false,
      INSECURE: false,
      NOT_FOUND: false,
      // A refusal looks standing but is not: the message sends the user to
      // their browser settings, so they need a way back in afterwards.
      DENIED: true,
      // Transients: the other app closes, the track comes back, the next frame
      // encodes.
      BUSY: true,
      INTERRUPTED: true,
      CAPTURE_FAILED: true,
      UNKNOWN: true,
    })
  })
})

describe('LiveCardViewfinder', () => {
  it('renders a muted, inline-playing video so a browser will start it without a gesture of its own', () => {
    const tree = render()
    const video = [...walkTree(tree)].find(node => node.type === 'video')

    expect(video).toBeDefined()
    expect(video.props.muted).toBe(true)
    expect(video.props.playsInline).toBe(true)
    expect(video.props.autoPlay).toBe(true)
  })

  it('never renders a file input: this path must not go near the OS camera app', () => {
    const tree = render()

    expect(findAll(tree, node => node.type === 'input')).toEqual([])
  })

  it('does not open the camera on mount, only from the start button', async () => {
    const tree = render()

    // Mounting creates the session and binds visibility, and stops there.
    expect(session()).toBeDefined()
    expect(session().start).not.toHaveBeenCalled()

    const start = buttonsWithText(tree)[0]
    expect(textOf(start)).toContain('scanner.startCamera')
    await start.props.onClick()

    expect(session().start).toHaveBeenCalledTimes(1)
  })

  it('binds the visibility listener to the real document', () => {
    render()

    expect(session().bindVisibility).toHaveBeenCalledWith(fakeDocument)
  })

  it('attaches the live stream to the video element and plays it', () => {
    const tree = render()
    attachVideo(tree)
    const stream = { id: 'stream-1' }

    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream })
    render()

    expect(fakeVideo.srcObject).toBe(stream)
    expect(fakeVideo.play).toHaveBeenCalledTimes(1)
  })

  it('stages one photo per shutter tap and keeps the camera running between taps', async () => {
    const first = new File(['one'], 'card-1.jpg', { type: 'image/jpeg' })
    const second = new File(['two'], 'card-2.jpg', { type: 'image/jpeg' })
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()
    session().capture.mockResolvedValueOnce(first).mockResolvedValueOnce(second)

    const shutter = buttonsWithText(tree)[0]
    await shutter.props.onClick()
    tree = render()
    await buttonsWithText(tree)[0].props.onClick()

    expect(session().capture).toHaveBeenCalledTimes(2)
    expect(session().capture).toHaveBeenCalledWith(fakeVideo)
    expect(props.onCapture.mock.calls).toEqual([[first], [second]])
    expect(session().stop).not.toHaveBeenCalled()
  })

  it('closes after one capture in single-shot mode', async () => {
    const file = new File(['one'], 'retake.jpg', { type: 'image/jpeg' })
    const onClose = vi.fn()
    let tree = render({ singleShot: true, onClose })
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render({ singleShot: true, onClose })
    session().capture.mockResolvedValueOnce(file)

    await buttonsWithText(tree)[0].props.onClick()

    expect(props.onCapture).toHaveBeenCalledWith(file)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('offers no shutter at all until the camera is live', () => {
    const tree = render()

    expect(textOf(tree)).toContain('scanner.startCamera')
    expect(textOf(tree)).not.toContain('scanner.captureCard')
  })

  it('stops capturing once the batch is full, without stopping the camera', async () => {
    let tree = render({ isFull: true })
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render({ isFull: true })

    const shutter = buttonsWithText(tree)[0]
    expect(shutter.props.disabled).toBe(true)
    await shutter.props.onClick()

    expect(session().capture).not.toHaveBeenCalled()
    expect(textOf(tree)).toContain('scanner.cameraBatchFull')
    expect(session().stop).not.toHaveBeenCalled()
  })

  it('explains a refusal and offers the retry that message depends on', () => {
    render()
    session().publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.DENIED, stream: null })
    const tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorDenied')
    expect(text).toContain('scanner.cameraFallbackHint')
    // The message says to allow the camera in browser settings. Without this
    // button there is nothing to press once they have.
    expect(text).toContain('scanner.retryCamera')
    expect(buttonsWithText(tree)).not.toEqual([])
  })

  it('offers a retry for a camera that is merely busy', async () => {
    render()
    session().publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.BUSY, stream: null })
    const tree = render()

    expect(textOf(tree)).toContain('scanner.retryCamera')
    await buttonsWithText(tree)[0].props.onClick()
    expect(session().start).toHaveBeenCalledTimes(1)
  })

  it('offers no retry on a device that has no camera to find', () => {
    render()
    session().publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.NOT_FOUND, stream: null })
    const tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorNotFound')
    expect(text).toContain('scanner.cameraFallbackHint')
    expect(text).not.toContain('scanner.retryCamera')
    expect(text).not.toContain('scanner.startCamera')
    expect(buttonsWithText(tree)).toEqual([])
  })

  it('offers no retry in a browser that cannot open a live camera', () => {
    render()
    session().publish({ status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.UNSUPPORTED, stream: null })
    const tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorUnsupported')
    expect(text).toContain('scanner.cameraFallbackHint')
    expect(text).not.toContain('scanner.retryCamera')
    expect(text).not.toContain('scanner.startCamera')
    expect(buttonsWithText(tree)).toEqual([])
  })

  // "A hidden tab keeps a standing failure on screen" is deliberately NOT
  // asserted here. The stand-in session mirrors that rule, so a test of it
  // against this file would pass whatever the real session did - it was
  // written, watched to pass while the shipped stop() was mutated, and removed.
  // LiveCardViewfinder.integration.test.js makes the claim against the real
  // session, where it can actually fail.

  it('reports an insecure origin on mount instead of dangling a dead start button', () => {
    camera.supportFailure = CAMERA_FAILURE.INSECURE
    render()
    const tree = render()

    expect(textOf(tree)).toContain('scanner.cameraErrorInsecure')
    expect(buttonsWithText(tree)).toEqual([])
    expect(session().start).not.toHaveBeenCalled()
  })

  it('keeps the camera live and says so when a single frame fails to encode', async () => {
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()
    session().capture.mockRejectedValueOnce(
      Object.assign(new Error('no blob'), { reason: CAMERA_FAILURE.CAPTURE_FAILED }),
    )

    await buttonsWithText(tree)[0].props.onClick()
    tree = render()

    expect(props.onCapture).not.toHaveBeenCalled()
    expect(textOf(tree)).toContain('scanner.cameraErrorCaptureFailed')
    expect(textOf(tree)).toContain('scanner.captureCard')
  })

  it('clears the failed frame message as soon as a later tap works', async () => {
    const recovered = new File(['ok'], 'card-2.jpg', { type: 'image/jpeg' })
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()
    session().capture
      .mockRejectedValueOnce(Object.assign(new Error('no blob'), { reason: CAMERA_FAILURE.CAPTURE_FAILED }))
      .mockResolvedValueOnce(recovered)

    await buttonsWithText(tree)[0].props.onClick()
    tree = render()
    expect(textOf(tree)).toContain('scanner.cameraErrorCaptureFailed')

    await buttonsWithText(tree)[0].props.onClick()
    tree = render()

    // One bad frame is a transient. Leaving "that frame could not be captured"
    // above a viewfinder that has since captured another card tells the user
    // their last tap failed when it did not.
    expect(props.onCapture).toHaveBeenCalledWith(recovered)
    expect(textOf(tree)).not.toContain('scanner.cameraErrorCaptureFailed')
    expect(textOf(tree)).toContain('scanner.captureCard')
  })

  it('keeps a failure the session itself raised while a frame was being drawn', async () => {
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()
    const file = new File(['ok'], 'card-1.jpg', { type: 'image/jpeg' })
    session().capture.mockImplementation(async () => {
      // The track ends while the canvas is being drawn: the frame is still
      // good, but the camera is gone and must not be reported as live.
      session().state = { status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.INTERRUPTED, stream: null }
      return file
    })

    await buttonsWithText(tree)[0].props.onClick()
    tree = render()

    expect(props.onCapture).toHaveBeenCalledWith(file)
    expect(textOf(tree)).toContain('scanner.cameraErrorInterrupted')
    expect(textOf(tree)).toContain('scanner.retryCamera')
  })

  it('drops out of live and offers a retry when the stream dies mid-capture', async () => {
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()
    session().capture.mockImplementation(async () => {
      session().state = { status: CAMERA_STATUS.ERROR, failure: CAMERA_FAILURE.INTERRUPTED, stream: null }
      throw Object.assign(new Error('track ended'), { reason: CAMERA_FAILURE.INTERRUPTED })
    })

    await buttonsWithText(tree)[0].props.onClick()
    tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorInterrupted')
    expect(text).toContain('scanner.retryCamera')
    expect(text).not.toContain('scanner.captureCard')
  })

  it('stops the camera from its own stop button', async () => {
    let tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    tree = render()

    const stop = buttonsWithText(tree)[1]
    await stop.props.onClick()

    expect(session().stop).toHaveBeenCalledTimes(1)
  })

  it('unbinds, detaches and disposes the session on unmount', () => {
    const tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    render()
    expect(fakeVideo.srcObject).toEqual({ id: 'stream-1' })

    hookHarness.unmount()

    expect(session().unbindVisibility).toHaveBeenCalledTimes(1)
    expect(session().dispose).toHaveBeenCalledTimes(1)
    expect(fakeVideo.srcObject).toBeNull()
  })

  it('detaches the stream from the element when the session goes idle', () => {
    const tree = render()
    attachVideo(tree)
    session().publish({ status: CAMERA_STATUS.LIVE, failure: null, stream: { id: 'stream-1' } })
    render()

    session().publish({ status: CAMERA_STATUS.IDLE, failure: null, stream: null })
    render()

    expect(fakeVideo.srcObject).toBeNull()
  })
})

describe('single-shot mode copy', () => {
  it('does not invite a batch when it replaces one photo and closes', () => {
    // Found on a device, not by reading: the re-take modal showed the staging
    // hint, "Keep going for as many cards as you like", in a viewfinder that
    // takes exactly one photo and then shuts.
    const tree = render({ singleShot: true })

    expect(textOf(tree)).toContain('scanner.liveViewfinderHintSingle')
    expect(textOf(tree)).not.toContain('scanner.liveViewfinderHint')
  })

  it('keeps the batch hint when it is staging a batch', () => {
    const tree = render({ singleShot: false })

    expect(textOf(tree)).toContain('scanner.liveViewfinderHint')
  })
})
