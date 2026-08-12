import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { hookHarness, findAll, walkTree } from '../test/hookHarness'
import { SCANNER_IMAGE_ACCEPT } from '../utils/scannerImages'

// The scanner's own state is the subject here - "two shutter taps stage two
// photos" is a statement about it - so the harness owns React's hooks and the
// component is called directly. Modal and ConfirmDialog are therefore never
// invoked, and need no stubbing.
vi.mock('react', async importOriginal => ({
  ...(await importOriginal()),
  ...hookHarness.hooks,
}))

const stubs = vi.hoisted(() => ({
  enqueueScanJob: vi.fn(),
  navigate: vi.fn(),
  toast: { success: vi.fn(), error: vi.fn() },
}))

vi.mock('../api/client', () => ({ enqueueScanJob: stubs.enqueueScanJob }))
vi.mock('react-hot-toast', () => ({ default: stubs.toast }))
vi.mock('react-router-dom', () => ({ useNavigate: () => stubs.navigate }))
vi.mock('../contexts/SettingsContext', () => ({ useSettings: () => ({ t: key => key }) }))
// The scanner now invalidates the shared queue on enqueue.
vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries: () => {} }) }))

const LiveCardViewfinder = (await import('./LiveCardViewfinder')).default
const UnifiedCardScanner = (await import('./UnifiedCardScanner')).default

let objectUrlCount
let revoked
let fileInputs

const render = (props = {}) => hookHarness.renderAndFlush(UnifiedCardScanner, {
  isOpen: true,
  onClose: stubs.onClose,
  ...props,
})

const jpeg = name => new File([`bytes-of-${name}`], name, { type: 'image/jpeg' })

const inputsOf = tree => findAll(tree, node => node.type === 'input')
const thumbnailsOf = tree => findAll(tree, node => node.type === 'img')
const viewfinderOf = tree => findAll(tree, node => node.type === LiveCardViewfinder)[0]
const removeButtonsOf = tree => findAll(
  tree,
  node => node.type === 'button' && node.props['aria-label'] === 'common.remove',
)

const textOf = node => [...walkTree(node)]
  .flatMap(entry => (Array.isArray(entry.props?.children) ? entry.props.children : [entry.props?.children]))
  .filter(child => typeof child === 'string')

/** The "<n>/50 photos" counter, joined back into one string. */
const stagedCountOf = tree => [...walkTree(tree)]
  .find(node => Array.isArray(node.props?.children) && node.props.children.includes('/50 '))
  .props.children.join('')

const buttonByText = (tree, label) => findAll(
  tree,
  node => node.type === 'button' && textOf(node).includes(label),
)[0]

const submitButtonOf = tree => buttonByText(tree, 'scanner.startScanning')

/**
 * Gives both hidden file inputs a stand-in element, so a test can prove the
 * viewfinder path never reaches for one.
 */
const armFileInputs = tree => {
  fileInputs = inputsOf(tree).map(node => {
    const ref = node.ref ?? node.props.ref
    ref.current = { click: vi.fn(), value: 'C:/fakepath/last.jpg' }
    return ref.current
  })
  return fileInputs
}

const selectFromInput = (input, files) => {
  const target = { files, value: 'C:/fakepath/chosen.jpg' }
  input.props.onChange({ target })
  return target
}

beforeEach(() => {
  hookHarness.reset()
  objectUrlCount = 0
  revoked = []
  fileInputs = []
  stubs.enqueueScanJob.mockReset()
  stubs.enqueueScanJob.mockResolvedValue({ id: 7 })
  stubs.navigate.mockReset()
  stubs.toast.success.mockReset()
  stubs.toast.error.mockReset()
  stubs.onClose = vi.fn()
  vi.stubGlobal('URL', {
    createObjectURL: () => {
      objectUrlCount += 1
      return `blob:staged-${objectUrlCount}`
    },
    revokeObjectURL: url => revoked.push(url),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('UnifiedCardScanner file-input fallbacks', () => {
  it('still offers the OS camera input and the multi-select gallery input, unchanged', () => {
    const tree = render()
    const [cameraInput, galleryInput] = inputsOf(tree)

    expect(cameraInput.props.type).toBe('file')
    expect(cameraInput.props.accept).toBe(SCANNER_IMAGE_ACCEPT)
    expect(cameraInput.props.capture).toBe('environment')
    expect(cameraInput.props.multiple).toBeUndefined()

    expect(galleryInput.props.type).toBe('file')
    expect(galleryInput.props.accept).toBe(SCANNER_IMAGE_ACCEPT)
    expect(galleryInput.props.multiple).toBe(true)
    expect(galleryInput.props.capture).toBeUndefined()
  })

  it('stages a gallery selection and clears the input so the same file can be picked again', () => {
    let tree = render()
    const target = selectFromInput(inputsOf(tree)[1], [jpeg('from-gallery.jpg')])
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(1)
    expect(target.value).toBe('')
  })

  it('stages an OS camera-app photo through the camera input', () => {
    let tree = render()
    selectFromInput(inputsOf(tree)[0], [jpeg('from-os-camera.jpg')])
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(1)
  })

  it('opens the OS camera app from Take photo, and the picker from Choose from gallery', () => {
    // These two buttons are the whole fallback: on a device where the live
    // viewfinder cannot run - a refused permission, a managed device with the
    // camera blocked by policy, a browser without getUserMedia - they are the
    // only way a card gets into the batch.
    const tree = render()
    const [cameraInput, galleryInput] = armFileInputs(tree)

    buttonByText(tree, 'scanner.takePhoto').props.onClick()

    expect(cameraInput.click).toHaveBeenCalledTimes(1)
    expect(galleryInput.click).not.toHaveBeenCalled()

    buttonByText(tree, 'scanner.chooseFromGallery').props.onClick()

    expect(galleryInput.click).toHaveBeenCalledTimes(1)
    expect(cameraInput.click).toHaveBeenCalledTimes(1)
  })

  it('stages a whole multi-file gallery selection, in the order it was chosen', async () => {
    let tree = render()

    selectFromInput(inputsOf(tree)[1], [jpeg('one.jpg'), jpeg('two.jpg'), jpeg('three.jpg')])
    tree = render()

    expect(thumbnailsOf(tree).map(node => node.props.src))
      .toEqual(['blob:staged-1', 'blob:staged-2', 'blob:staged-3'])
    expect(stagedCountOf(tree)).toBe('3/50 scanner.photos')

    await submitButtonOf(tree).props.onClick()

    // Order is not cosmetic: individual-scan positions are indexes into it.
    expect(stubs.enqueueScanJob.mock.calls[0][0].map(file => file.name))
      .toEqual(['one.jpg', 'two.jpg', 'three.jpg'])
  })

  it('leaves both fallback buttons usable right up to the cap, then disables them', () => {
    let tree = render()
    selectFromInput(inputsOf(tree)[1], Array.from({ length: 49 }, (_, index) => jpeg(`bulk-${index}.jpg`)))
    tree = render()

    expect(buttonByText(tree, 'scanner.takePhoto').props.disabled).toBe(false)
    expect(buttonByText(tree, 'scanner.chooseFromGallery').props.disabled).toBe(false)

    selectFromInput(inputsOf(tree)[0], [jpeg('fiftieth.jpg')])
    tree = render()

    expect(stagedCountOf(tree)).toBe('50/50 scanner.photos')
    expect(buttonByText(tree, 'scanner.takePhoto').props.disabled).toBe(true)
    expect(buttonByText(tree, 'scanner.chooseFromGallery').props.disabled).toBe(true)
  })

  it('rejects a file type the scanner cannot send', () => {
    let tree = render()
    selectFromInput(inputsOf(tree)[1], [new File(['x'], 'card.gif', { type: 'image/gif' })])
    tree = render()

    expect(thumbnailsOf(tree)).toEqual([])
    expect(stubs.toast.error).toHaveBeenCalledWith('scanner.unsupportedImage')
  })
})

describe('UnifiedCardScanner live capture', () => {
  it('hands the viewfinder a capture sink and the batch-full flag', () => {
    const tree = render()
    const viewfinder = viewfinderOf(tree)

    expect(viewfinder).toBeDefined()
    expect(typeof viewfinder.props.onCapture).toBe('function')
    expect(viewfinder.props.isFull).toBe(false)
  })

  it('stages a captured JPEG immediately, with no file input involved', () => {
    let tree = render()
    armFileInputs(tree)

    viewfinderOf(tree).props.onCapture(jpeg('captured.jpg'))
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(1)
    expect(thumbnailsOf(tree)[0].props.src).toBe('blob:staged-1')
    expect(stagedCountOf(tree)).toBe('1/50 scanner.photos')
    for (const input of fileInputs) expect(input.click).not.toHaveBeenCalled()
  })

  it('stages two distinct photos from two shutter taps, and sends nothing until Done', () => {
    let tree = render()
    armFileInputs(tree)

    viewfinderOf(tree).props.onCapture(jpeg('first.jpg'))
    tree = render()
    viewfinderOf(tree).props.onCapture(jpeg('second.jpg'))
    tree = render()

    const previews = thumbnailsOf(tree).map(node => node.props.src)
    expect(previews).toEqual(['blob:staged-1', 'blob:staged-2'])
    expect(stagedCountOf(tree)).toBe('2/50 scanner.photos')
    for (const input of fileInputs) expect(input.click).not.toHaveBeenCalled()
    // Nothing is uploaded per capture: the batch is one request, at Done.
    expect(stubs.enqueueScanJob).not.toHaveBeenCalled()
  })

  it('keeps staged photos when the camera stops, because the batch is not the camera session', () => {
    let tree = render()
    viewfinderOf(tree).props.onCapture(jpeg('first.jpg'))
    tree = render()
    viewfinderOf(tree).props.onCapture(jpeg('second.jpg'))
    tree = render()

    // The viewfinder unmounting or erroring only ever changes its own props.
    tree = render()
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(2)
    expect(revoked).toEqual([])
  })

  it('stops accepting captures at the 50-photo cap and tells the viewfinder the batch is full', () => {
    let tree = render()
    selectFromInput(inputsOf(tree)[1], Array.from({ length: 50 }, (_, index) => jpeg(`bulk-${index}.jpg`)))
    tree = render()
    expect(thumbnailsOf(tree)).toHaveLength(50)
    expect(viewfinderOf(tree).props.isFull).toBe(true)

    viewfinderOf(tree).props.onCapture(jpeg('fifty-first.jpg'))
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(50)
    expect(stubs.toast.error).toHaveBeenCalledWith('scanner.batchLimitReached')
  })
})

describe('UnifiedCardScanner submission', () => {
  const stageThree = () => {
    let tree = render()
    for (const name of ['a.jpg', 'b.jpg', 'c.jpg']) {
      viewfinderOf(tree).props.onCapture(jpeg(name))
      tree = render()
    }
    return tree
  }

  it('sends one request carrying every staged photo, in order', async () => {
    const tree = stageThree()

    await submitButtonOf(tree).props.onClick()

    expect(stubs.enqueueScanJob).toHaveBeenCalledTimes(1)
    const [files, individualPositions] = stubs.enqueueScanJob.mock.calls[0]
    expect(files.map(file => file.name)).toEqual(['a.jpg', 'b.jpg', 'c.jpg'])
    expect(individualPositions).toEqual([])
    expect(stubs.navigate).toHaveBeenCalledWith('/scans/7')
  })

  it('excludes only the deleted photo', async () => {
    let tree = stageThree()

    removeButtonsOf(tree)[1].props.onClick()
    tree = render()

    expect(thumbnailsOf(tree)).toHaveLength(2)
    expect(revoked).toEqual(['blob:staged-2'])

    await submitButtonOf(tree).props.onClick()

    expect(stubs.enqueueScanJob).toHaveBeenCalledTimes(1)
    expect(stubs.enqueueScanJob.mock.calls[0][0].map(file => file.name)).toEqual(['a.jpg', 'c.jpg'])
  })

  it('carries the individual-scan positions of the photos the user marked', async () => {
    let tree = stageThree()

    findAll(tree, node => node.type === 'button' && textOf(node).includes('scanner.scanIndividually'))[1]
      .props.onClick()
    tree = render()

    await submitButtonOf(tree).props.onClick()

    expect(stubs.enqueueScanJob.mock.calls[0][1]).toEqual([1])
  })

  it('refuses to submit an empty batch', async () => {
    const tree = render()

    expect(submitButtonOf(tree).props.disabled).toBe(true)
    await submitButtonOf(tree).props.onClick()

    expect(stubs.enqueueScanJob).not.toHaveBeenCalled()
  })

  it('leaves a reopened scanner alone when an abandoned submission lands', async () => {
    // The generation guard. Without it the toast is skipped, the new batch is
    // cleared and the user is navigated away from photos they just took.
    let resolveJob
    stubs.enqueueScanJob.mockImplementation(() => new Promise(resolve => { resolveJob = resolve }))
    let tree = stageThree()

    const submission = submitButtonOf(tree).props.onClick()
    render({ isOpen: false })
    tree = render({ isOpen: true })
    resolveJob({ id: 7 })
    await submission

    expect(stubs.toast.success).toHaveBeenCalledWith('scanner.batchQueuedInBackground')
    expect(stubs.navigate).not.toHaveBeenCalled()
    expect(thumbnailsOf(tree)).toHaveLength(3)
  })

  it('surfaces the API reason and keeps the batch when the upload fails', async () => {
    stubs.enqueueScanJob.mockRejectedValue({ response: { data: { detail: 'Too many photos' } } })
    let tree = stageThree()

    await submitButtonOf(tree).props.onClick()
    tree = render()

    expect(stubs.toast.error).toHaveBeenCalledWith('Too many photos')
    expect(thumbnailsOf(tree)).toHaveLength(3)
    expect(stubs.navigate).not.toHaveBeenCalled()
  })
})
