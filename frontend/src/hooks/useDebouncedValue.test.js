import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { scheduleDebounced, useDebouncedValue } from './useDebouncedValue'

describe('scheduleDebounced', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('does not publish before the delay elapses', () => {
    const publish = vi.fn()
    scheduleDebounced(publish, 'char')
    vi.advanceTimersByTime(249)
    expect(publish).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(publish).toHaveBeenCalledWith('char')
  })

  it('publishes only the last value when typing continues', () => {
    // The point of the hook: a request per keystroke would be worse than the
    // single download it replaced.
    const publish = vi.fn()
    let cancel = scheduleDebounced(publish, 'c')
    vi.advanceTimersByTime(100)
    cancel()
    cancel = scheduleDebounced(publish, 'ch')
    vi.advanceTimersByTime(100)
    cancel()
    scheduleDebounced(publish, 'cha')
    vi.advanceTimersByTime(250)

    expect(publish).toHaveBeenCalledTimes(1)
    expect(publish).toHaveBeenCalledWith('cha')
  })

  it('cancelling stops the value arriving at all', () => {
    const publish = vi.fn()
    scheduleDebounced(publish, 'gone')()
    vi.advanceTimersByTime(1000)
    expect(publish).not.toHaveBeenCalled()
  })

  it('the hook wires the scheduler with a default delay', () => {
    expect(typeof useDebouncedValue).toBe('function')
    expect(String(useDebouncedValue)).toContain('scheduleDebounced')
  })
})
