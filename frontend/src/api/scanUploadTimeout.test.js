/**
 * Guards a live failure where one upload produced two scan jobs.
 *
 * The client sets a 30 second timeout for every request. A scan upload carries
 * the camera's original files, and eleven of them exceeded that on a domestic
 * upstream. The browser abandoned the request and reported a failure, while
 * the SERVER carried on and created the job. Retrying produced a second job
 * holding the same photographs, and the abandoned first one sat in the review
 * queue asking for decisions on cards that had already been filed.
 *
 * Testing the helper alone does not catch this: removing the timeout from the
 * request config leaves every arithmetic assertion passing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const post = vi.fn(() => Promise.resolve({ data: {} }))

vi.mock('axios', () => ({
  default: {
    create: () => ({
      post,
      delete: vi.fn(() => Promise.resolve({ data: {} })),
      get: vi.fn(() => Promise.resolve({ data: {} })),
      put: vi.fn(() => Promise.resolve({ data: {} })),
      interceptors: {
        request: { use: vi.fn() },
        response: { use: vi.fn() },
      },
    }),
  },
}))

const { enqueueScanJob, scanUploadTimeoutMs } = await import('./client')

const photo = (megabytes) =>
  new File([new Uint8Array(megabytes * 1024 * 1024)], 'scan.jpg', { type: 'image/jpeg' })

describe('enqueueScanJob', () => {
  beforeEach(() => post.mockClear())

  it('sends a timeout far longer than the client default', () => {
    enqueueScanJob([photo(1)], [])
    const [, , config] = post.mock.calls[0]
    expect(config?.timeout).toBeGreaterThan(30_000)
  })

  it('scales that timeout with the photos actually being sent', () => {
    enqueueScanJob([photo(1)], [])
    const small = post.mock.calls[0][2].timeout
    post.mockClear()
    enqueueScanJob(Array.from({ length: 11 }, () => photo(4)), [])
    const large = post.mock.calls[0][2].timeout
    expect(large).toBeGreaterThan(small)
  })

  it('still overrides the JSON content type', () => {
    enqueueScanJob([photo(1)], [])
    const [, , config] = post.mock.calls[0]
    expect(config?.headers?.['Content-Type']).toBe('multipart/form-data')
  })
})

describe('scanUploadTimeoutMs', () => {
  it('never gives an upload less than two minutes', () => {
    expect(scanUploadTimeoutMs([{ size: 1024 }])).toBe(120_000)
    expect(scanUploadTimeoutMs([])).toBe(120_000)
  })

  it('is bounded, so a genuinely stuck request still fails', () => {
    expect(scanUploadTimeoutMs(Array(50).fill({ size: 15 * 1024 * 1024 }))).toBe(20 * 60_000)
  })

  it('tolerates a file with no size rather than producing NaN', () => {
    expect(scanUploadTimeoutMs([{}, null, undefined])).toBe(120_000)
  })
})
