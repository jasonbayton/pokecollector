import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { hookHarness, findAll, walkTree } from '../test/hookHarness'

/**
 * The component driven against the real camera session.
 *
 * LiveCardViewfinder.test.js replaces createCameraSession with a hand-written
 * stand-in, so it can only prove the component behaves against the seam a test
 * author wrote down. Nothing there would notice the session and the component
 * disagreeing. Here the only mocks are React's hooks (the harness) and the
 * translation lookup; utils/cameraCapture is the shipped module, and the
 * browser it reaches for - navigator.mediaDevices, document, a canvas, a
 * MediaStreamTrack - is stubbed at the global boundary instead.
 *
 * This is the closest an environment with no DOM and no camera can get to the
 * device test that DISALLOW_CAMERA on the test handset makes impossible.
 */

vi.mock('react', async importOriginal => ({
  ...(await importOriginal()),
  ...hookHarness.hooks,
}))

vi.mock('../contexts/SettingsContext', () => ({
  useSettings: () => ({ t: key => key }),
}))

const LiveCardViewfinder = (await import('./LiveCardViewfinder')).default

function createTrack() {
  const listeners = new Map()
  const track = {
    kind: 'video',
    stopCount: 0,
    stop() { track.stopCount += 1 },
    addEventListener(type, handler) {
      listeners.set(type, [...(listeners.get(type) || []), handler])
    },
    removeEventListener(type, handler) {
      listeners.set(type, (listeners.get(type) || []).filter(entry => entry !== handler))
    },
    emit(type) {
      for (const handler of [...(listeners.get(type) || [])]) handler()
    },
  }
  return track
}

let track
let stream
let getUserMedia
let documentListeners
let fakeDocument
let fakeVideo
let canvases
let drawn
let encodings
let props

const render = (overrides = {}) => hookHarness.renderAndFlush(LiveCardViewfinder, { ...props, ...overrides })

const buttonsOf = tree => findAll(tree, node => node.type === 'button')

const textOf = tree => [...walkTree(tree)]
  .flatMap(node => (Array.isArray(node.props?.children) ? node.props.children : [node.props?.children]))
  .filter(child => typeof child === 'string')

/** Attaches a stand-in <video> to the ref the component just handed React. */
const attachVideo = tree => {
  const video = [...walkTree(tree)].find(node => node.type === 'video')
  const ref = video.ref ?? video.props.ref
  ref.current = fakeVideo
}

const hideTab = () => {
  fakeDocument.hidden = true
  documentListeners.get('visibilitychange')()
}

/** Mount, attach the element, start the camera, and hand back the live tree. */
const startCamera = async () => {
  let tree = render()
  attachVideo(tree)
  await buttonsOf(tree)[0].props.onClick()
  return render()
}

beforeEach(() => {
  hookHarness.reset()
  track = createTrack()
  stream = { id: 'real-stream', getTracks: () => [track] }
  getUserMedia = vi.fn().mockResolvedValue(stream)
  canvases = []
  drawn = []
  encodings = []
  documentListeners = new Map()
  fakeDocument = {
    hidden: false,
    addEventListener: (type, handler) => documentListeners.set(type, handler),
    removeEventListener: type => documentListeners.delete(type),
    createElement: tag => {
      if (tag !== 'canvas') throw new Error(`The capture path asked for an unexpected <${tag}>.`)
      const canvas = {
        width: 0,
        height: 0,
        getContext: kind => (kind === '2d' ? { drawImage: (...args) => drawn.push(args) } : null),
        toBlob: (callback, type, quality) => {
          encodings.push({ type, quality })
          callback(new Blob([`jpeg-bytes-${encodings.length}`], { type }))
        },
      }
      canvases.push(canvas)
      return canvas
    },
  }
  fakeVideo = {
    videoWidth: 1280,
    videoHeight: 960,
    srcObject: undefined,
    play: vi.fn(() => Promise.resolve()),
  }
  props = { onCapture: vi.fn(), isFull: false }
  vi.stubGlobal('document', fakeDocument)
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('navigator', { mediaDevices: { getUserMedia } })
})

afterEach(() => {
  hookHarness.unmount()
  vi.unstubAllGlobals()
})

describe('LiveCardViewfinder against the real camera session', () => {
  it('touches no camera until the user asks, then opens the rear one', async () => {
    const tree = render()

    expect(getUserMedia).not.toHaveBeenCalled()
    expect(textOf(tree)).toContain('scanner.startCamera')

    await buttonsOf(tree)[0].props.onClick()

    expect(getUserMedia).toHaveBeenCalledTimes(1)
    expect(getUserMedia).toHaveBeenCalledWith({
      audio: false,
      video: {
        facingMode: { ideal: 'environment' },
        width: { ideal: 1920 },
        height: { ideal: 1080 },
      },
    })
  })

  it('attaches the stream the browser handed back and plays it', async () => {
    const tree = await startCamera()

    expect(fakeVideo.srcObject).toBe(stream)
    expect(fakeVideo.play).toHaveBeenCalledTimes(1)
    expect(textOf(tree)).toContain('scanner.captureCard')
    expect(textOf(tree)).toContain('scanner.cameraLive')
  })

  it('stages a real JPEG per tap, at sensor resolution, without stopping the camera', async () => {
    let tree = await startCamera()

    await buttonsOf(tree)[0].props.onClick()
    tree = render()
    await buttonsOf(tree)[0].props.onClick()
    tree = render()

    const [first, second] = props.onCapture.mock.calls.map(([file]) => file)
    expect(props.onCapture).toHaveBeenCalledTimes(2)
    expect(first).toBeInstanceOf(File)
    expect(first.type).toBe('image/jpeg')
    expect(first.name).toMatch(/^card-.+\.jpg$/)
    expect(first.name).not.toBe(second.name)
    expect(await first.text()).not.toBe(await second.text())
    // Drawn at the stream's own frame size, not the letterboxed CSS box.
    expect(drawn).toEqual([[fakeVideo, 0, 0, 1280, 960], [fakeVideo, 0, 0, 1280, 960]])
    expect(canvases.map(canvas => [canvas.width, canvas.height])).toEqual([[1280, 960], [1280, 960]])
    expect(encodings).toEqual([
      { type: 'image/jpeg', quality: 0.92 },
      { type: 'image/jpeg', quality: 0.92 },
    ])
    // One tap is one card: the camera survives, and the fallback file inputs
    // are never involved.
    expect(track.stopCount).toBe(0)
    expect(findAll(tree, node => node.type === 'input')).toEqual([])
  })

  it('says a frame failed, keeps the camera, and clears the message on the next good tap', async () => {
    let tree = await startCamera()
    fakeVideo.videoWidth = 0
    fakeVideo.videoHeight = 0

    await buttonsOf(tree)[0].props.onClick()
    tree = render()

    expect(props.onCapture).not.toHaveBeenCalled()
    expect(textOf(tree)).toContain('scanner.cameraErrorInterrupted')
    expect(textOf(tree)).toContain('scanner.captureCard')

    fakeVideo.videoWidth = 1280
    fakeVideo.videoHeight = 960
    await buttonsOf(tree)[0].props.onClick()
    tree = render()

    expect(props.onCapture).toHaveBeenCalledTimes(1)
    expect(textOf(tree)).not.toContain('scanner.cameraErrorInterrupted')
    expect(track.stopCount).toBe(0)
  })

  it('releases the camera when the tab is hidden, and offers to start it again', async () => {
    await startCamera()

    hideTab()
    const tree = render()

    expect(track.stopCount).toBe(1)
    expect(fakeVideo.srcObject).toBeNull()
    expect(textOf(tree)).toContain('scanner.startCamera')
  })

  it('still explains a refusal after the tab has been hidden, and never re-prompts', async () => {
    getUserMedia.mockRejectedValue(Object.assign(new Error('no'), { name: 'NotAllowedError' }))
    let tree = render()
    await buttonsOf(tree)[0].props.onClick()
    tree = render()
    expect(textOf(tree)).toContain('scanner.cameraErrorDenied')

    hideTab()
    tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorDenied')
    expect(text).toContain('scanner.cameraFallbackHint')
    expect(text).not.toContain('scanner.startCamera')
    expect(buttonsOf(tree)).toEqual([])
    expect(getUserMedia).toHaveBeenCalledTimes(1)
  })

  it('still explains an insecure origin after the tab has been hidden', () => {
    vi.stubGlobal('isSecureContext', false)
    render()
    let tree = render()
    expect(textOf(tree)).toContain('scanner.cameraErrorInsecure')

    hideTab()
    tree = render()

    expect(textOf(tree)).toContain('scanner.cameraErrorInsecure')
    expect(buttonsOf(tree)).toEqual([])
    expect(getUserMedia).not.toHaveBeenCalled()
  })

  it('reports an interruption and offers a retry when the track ends under it', async () => {
    await startCamera()

    track.emit('ended')
    const tree = render()

    const text = textOf(tree)
    expect(text).toContain('scanner.cameraErrorInterrupted')
    expect(text).toContain('scanner.retryCamera')
    expect(fakeVideo.srcObject).toBeNull()
  })

  it('stops the track on unmount, so the camera light goes out', async () => {
    await startCamera()

    hookHarness.unmount()

    expect(track.stopCount).toBe(1)
    expect(fakeVideo.srcObject).toBeNull()
    expect(documentListeners.has('visibilitychange')).toBe(false)
  })
})
