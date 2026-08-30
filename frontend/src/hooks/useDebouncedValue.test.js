import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// React is stubbed so the hook's own body runs here. Rendering is impossible
// without jsdom, and a test that reimplements the hook's logic proves nothing
// about the hook - an earlier version of this file did exactly that.
const hookState = { value: undefined, effect: null, deps: null }
vi.mock('react', () => ({
  useState: (initial) => [hookState.value === undefined ? initial : hookState.value,
                          (next) => { hookState.value = next }],
  useEffect: (effect, deps) => { hookState.effect = effect; hookState.deps = deps },
}))

const { scheduleDebounced, useDebouncedValue } = await import('./useDebouncedValue')

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
    const publish = vi.fn()
    let cancel = scheduleDebounced(publish, 'c')
    vi.advanceTimersByTime(100); cancel()
    cancel = scheduleDebounced(publish, 'ch')
    vi.advanceTimersByTime(100); cancel()
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
})

describe('useDebouncedValue', () => {
  beforeEach(() => { vi.useFakeTimers(); hookState.value = undefined; hookState.effect = null })
  afterEach(() => vi.useRealTimers())

  it('returns the current value and schedules the next through the effect', () => {
    expect(useDebouncedValue('first')).toBe('first')
    expect(typeof hookState.effect).toBe('function')

    hookState.effect()
    vi.advanceTimersByTime(250)
    expect(hookState.value).toBe('first')
  })

  it('the effect returns a cleanup that cancels the pending publication', () => {
    // Without this, a hook that scheduled but returned nothing would still
    // pass every other test here while restoring a request per keystroke.
    useDebouncedValue('typed')
    const cleanup = hookState.effect()
    expect(typeof cleanup).toBe('function')

    cleanup()
    vi.advanceTimersByTime(1000)
    expect(hookState.value).toBeUndefined()
  })

  it('re-runs the effect when the value or the delay changes', () => {
    useDebouncedValue('a', 400)
    expect(hookState.deps).toEqual(['a', 400])
  })
})
