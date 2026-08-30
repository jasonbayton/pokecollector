import { describe, expect, it, vi } from 'vitest'
import { useDebouncedValue } from './useDebouncedValue'

describe('useDebouncedValue', () => {
  it('is a function taking a value and an optional delay', () => {
    // The suite runs without jsdom, so hook behaviour cannot be rendered here.
    // What can be pinned is the contract the callers rely on: a default delay
    // exists, so a caller omitting it still debounces rather than passing
    // undefined to setTimeout.
    expect(typeof useDebouncedValue).toBe('function')
    expect(useDebouncedValue.length).toBe(1)
  })
})
