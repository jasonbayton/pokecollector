import { useEffect, useState } from 'react'

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

  useEffect(() => {
    const timer = setTimeout(() => setSettled(value), delay)
    return () => clearTimeout(timer)
  }, [value, delay])

  return settled
}
