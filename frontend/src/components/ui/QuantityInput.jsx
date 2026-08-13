import { useEffect, useState } from 'react'

/**
 * A quantity field you can actually clear while typing.
 *
 * Every quantity input in the app used to be written as
 *
 *   value={quantity}
 *   onChange={e => setQuantity(parseInt(e.target.value) || 1)}
 *
 * and `parseInt('')` is NaN, so `|| 1` put a 1 straight back the instant the
 * field was emptied. The digit could never be deleted, which on a phone means
 * typing 4 over a 1 leaves you with 14 or 41 rather than 4. The only way to
 * reach a number was to enter the wrong one and then correct it.
 *
 * So the text being edited is kept as text, and only turned into a number when
 * the user leaves the field or the form is read. Empty is a legitimate state
 * mid-edit; it is not a legitimate value, which is why it normalises on blur.
 */
export default function QuantityInput({
  value,
  onChange,
  min = 1,
  id,
  className = 'input',
  'aria-label': ariaLabel,
}) {
  const [text, setText] = useState(String(value ?? min))

  // Follow the value when the owner changes it (a different card selected, a
  // form reset), but never while the user is mid-edit with the field empty.
  useEffect(() => {
    if (text === '') return
    if (normalise(text, min) !== value) setText(String(value ?? min))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])

  const commit = raw => {
    const next = normalise(raw, min)
    setText(String(next))
    if (next !== value) onChange(next)
  }

  return (
    <input
      id={id}
      type="number"
      inputMode="numeric"
      min={min}
      value={text}
      aria-label={ariaLabel}
      className={className}
      onChange={event => {
        const raw = event.target.value
        setText(raw)
        // Report every usable value as it is typed, so a caller that submits
        // without blurring still gets what is on screen. An empty field
        // reports nothing and waits.
        if (raw !== '') {
          const next = normalise(raw, min)
          if (next !== value) onChange(next)
        }
      }}
      onBlur={event => commit(event.target.value)}
    />
  )
}

export function normalise(raw, min = 1) {
  const parsed = Number.parseInt(raw, 10)
  if (!Number.isFinite(parsed)) return min
  return Math.max(min, parsed)
}
