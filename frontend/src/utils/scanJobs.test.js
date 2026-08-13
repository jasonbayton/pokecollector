import { describe, expect, it } from 'vitest'
import {
  hasActiveScanJobs,
  isScanJobActive,
  scanAttentionCount,
  scanJobPollInterval,
  scanJobRemaining,
  addBusyItem,
  removeBusyItem,
} from './scanJobs'

describe('scan job presentation helpers', () => {
  it('treats only work in progress as active', () => {
    expect(isScanJobActive({ active: 2 })).toBe(true)
    expect(isScanJobActive({ active: 0, attention: 4 })).toBe(false)
  })

  it('polls active jobs and stops for completed jobs', () => {
    expect(scanJobPollInterval({ active: 1 })).toBe(3000)
    expect(scanJobPollInterval({ active: 0 })).toBe(false)
  })

  it('counts only actionable results for badges', () => {
    expect(scanAttentionCount([{ attention: 3 }, { attention: 2 }, { active: 50 }])).toBe(5)
  })

  it('detects active jobs across the inbox', () => {
    expect(hasActiveScanJobs([{ active: 0 }, { active: 1 }])).toBe(true)
    expect(hasActiveScanJobs([{ active: 0 }])).toBe(false)
  })
})

describe('scanJobRemaining', () => {
  it('sums the three outstanding counters', () => {
    expect(scanJobRemaining({ pending: 2, processing: 1, retrying: 3 })).toBe(6)
  })

  it('treats a missing counter as zero rather than rendering NaN at the user', () => {
    // A payload written before a counter existed, or truncated by a failed
    // refetch, used to reach the page as the literal text "NaN remaining".
    expect(scanJobRemaining({ pending: 2 })).toBe(2)
    expect(scanJobRemaining({})).toBe(0)
    expect(scanJobRemaining(null)).toBe(0)
    expect(scanJobRemaining(undefined)).toBe(0)
  })

  it('does not treat a non-numeric counter as NaN either', () => {
    expect(scanJobRemaining({ pending: 'x', processing: 2 })).toBe(2)
  })
})

describe('the in-flight item set', () => {
  it('keeps an earlier item gated when a second one starts', () => {
    // The defect: the gate was derived from a mutation's current variables,
    // which are replaced as soon as mutate() is called again. Starting item B
    // un-gated item A while A's re-take was still in flight, handing back the
    // candidates belonging to the photo A was replacing.
    const busy = addBusyItem(addBusyItem([], 1), 2)

    expect(busy).toContain(1)
    expect(busy).toContain(2)
  })

  it('releases only the item that settled', () => {
    const busy = removeBusyItem([1, 2], 2)

    expect(busy).toEqual([1])
  })

  it('does not list the same item twice if it is marked again', () => {
    expect(addBusyItem([1], 1)).toEqual([1])
  })
})
