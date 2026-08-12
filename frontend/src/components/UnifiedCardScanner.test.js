import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import UnifiedCardScanner from './UnifiedCardScanner'

const { enqueueScanJob, navigate, toastMock, stagedFiles, invalidateQueries } = vi.hoisted(() => ({
  enqueueScanJob: vi.fn(),
  invalidateQueries: vi.fn(),
  navigate: vi.fn(),
  toastMock: { success: vi.fn(), error: vi.fn() },
  stagedFiles: [],
}))

// The repository's tests render on the server, where a state update after the
// render is a no-op, so photos cannot be staged by clicking. Seeding the one
// array-valued initial state hands the component the batch a user would have
// taken, without touching hook order or the dispatcher.
vi.mock('react', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    useState: initial => actual.useState(
      Array.isArray(initial) && initial.length === 0 && stagedFiles.length ? stagedFiles : initial,
    ),
  }
})

vi.mock('../api/client', () => ({ enqueueScanJob }))
vi.mock('react-router-dom', () => ({ useNavigate: () => navigate }))
vi.mock('react-hot-toast', () => ({ default: toastMock }))
vi.mock('../contexts/SettingsContext', () => ({ useSettings: () => ({ t: key => key }) }))
vi.mock('@tanstack/react-query', () => ({ useQueryClient: () => ({ invalidateQueries }) }))

function* walk(node) {
  if (Array.isArray(node)) {
    for (const child of node) yield* walk(child)
    return
  }
  if (!node || typeof node !== 'object') return
  yield node
  yield* walk(node.props?.children)
}

const scannerTree = (props = {}) => {
  let captured
  function Capture() {
    captured = UnifiedCardScanner({ isOpen: true, onClose: () => {}, ...props })
    return null
  }
  renderToStaticMarkup(createElement(Capture))
  return [...walk(captured)]
}

const findStartButton = tree => tree.find(node => (
  node.type === 'button' && [...walk(node.props?.children)].some(child => child.props?.children === 'scanner.startScanning')
))

const photo = (id, individual) => ({ id, file: `file-${id}`, previewUrl: `blob:${id}`, individual })

beforeEach(() => {
  stagedFiles.length = 0
  enqueueScanJob.mockReset()
  enqueueScanJob.mockResolvedValue({ id: 'job-7' })
  navigate.mockReset()
  toastMock.success.mockReset()
  toastMock.error.mockReset()
  invalidateQueries.mockReset()
})

describe('UnifiedCardScanner submission', () => {
  it('submits the staged batch and takes the user to the new job', async () => {
    // The control for moving the scanner out of the card search: wherever it is
    // mounted, it must still enqueue the photos and land on the job page.
    stagedFiles.push(photo('a', false), photo('b', true))
    const onClose = vi.fn()

    await findStartButton(scannerTree({ onClose })).props.onClick()

    expect(enqueueScanJob).toHaveBeenCalledWith(['file-a', 'file-b'], [1])
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(navigate).toHaveBeenCalledWith('/scans/job-7')
  })

  it('tells the shared queue a job now exists, so the badge can find it', async () => {
    // The queue query only polls while its cached list already holds an active
    // job. A list fetched empty a moment ago stays cached, so without this the
    // new job is invisible in the badge until some unrelated refetch.
    stagedFiles.push(photo('a', false))

    await findStartButton(scannerTree()).props.onClick()

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scan-jobs'] })
  })

  it('still tells the queue when the user abandoned the scanner mid-submit', async () => {
    // The job exists either way, and on this path the user is never taken to
    // it, so the badge is the only thing that can tell them.
    stagedFiles.push(photo('a', false))
    const tree = scannerTree({ isOpen: false })

    await findStartButton(tree).props.onClick()

    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['scan-jobs'] })
  })

  it('reports a rejected batch instead of navigating away', async () => {
    stagedFiles.push(photo('a', false))
    enqueueScanJob.mockRejectedValue({ response: { data: { detail: 'Too many photos' } } })
    const onClose = vi.fn()

    await findStartButton(scannerTree({ onClose })).props.onClick()

    expect(toastMock.error).toHaveBeenCalledWith('Too many photos')
    expect(navigate).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('submits nothing when no photo is staged', async () => {
    const button = findStartButton(scannerTree())

    expect(button.props.disabled).toBe(true)
    await button.props.onClick()

    expect(enqueueScanJob).not.toHaveBeenCalled()
  })
})
