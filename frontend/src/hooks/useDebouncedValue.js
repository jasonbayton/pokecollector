import { useEffect, useState } from 'react'

/**
 * Publish a value once the delay has elapsed with no newer value.
 *
 * Split out of the hook so it can be tested for real. This suite runs without
 * jsdom, so a hook cannot be rendered; a test that reimplements the timer
 * inside itself passes whatever the hook does, which is worse than no test.
 */
export function scheduleDebounced(publish, value, delay = 250) {
  const timer = setTimeout(() => publish(value), delay)
  return () => clearTimeout(timer)
}

/**
 * A value that settles after typing stops.
 *
 * The trade and binder pickers now search on the server, so without this every
 * keystroke is a request. The previous behaviour was one download and then
 * local filtering, so an un-debounced search would be a worse experience than
 * the thing it replaced, not a better one.
 */
export function useDebouncedValue(value, delay = 250) {
  const [settled, setSettled] = useState(value)
  useEffect(() => scheduleDebounced(setSettled, value, delay), [value, delay])
  return settled
}
