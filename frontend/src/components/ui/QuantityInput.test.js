import { afterEach, describe, expect, it, vi } from 'vitest'

import { hookHarness, findOne } from '../../test/hookHarness'

vi.mock('react', async importOriginal => ({
  ...(await importOriginal()),
  ...hookHarness.hooks,
}))

const QuantityInput = (await import('./QuantityInput')).default
const { normalise } = await import('./QuantityInput')

afterEach(() => {
  hookHarness.reset()
})

const render = (props = {}) => {
  const tree = hookHarness.renderAndFlush(QuantityInput, { value: 1, onChange: () => {}, ...props })
  return findOne(tree, node => node.type === 'input')
}

describe('QuantityInput', () => {
  it('lets the field be emptied instead of snapping back to one', () => {
    // The whole point. Every one of these fields used to be
    // onChange={e => setQuantity(parseInt(e.target.value) || 1)}, and
    // parseInt('') is NaN, so clearing the box put a 1 straight back into it.
    // On a phone that means you cannot type 4 over a 1: you get 14 or 41.
    const onChange = vi.fn()
    let input = render({ value: 1, onChange })

    input.props.onChange({ target: { value: '' } })
    input = render({ value: 1, onChange })

    expect(input.props.value).toBe('')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('reports a typed number without waiting for blur', () => {
    const onChange = vi.fn()
    const input = render({ value: 1, onChange })

    input.props.onChange({ target: { value: '4' } })

    expect(onChange).toHaveBeenCalledWith(4)
  })

  it('settles an empty field back to the minimum when the user leaves it', () => {
    // Empty is a legitimate thing to be typing. It is not a legitimate value.
    const onChange = vi.fn()
    let input = render({ value: 1, onChange })

    input.props.onChange({ target: { value: '' } })
    input = render({ value: 1, onChange })
    input.props.onBlur({ target: { value: '' } })
    input = render({ value: 1, onChange })

    expect(input.props.value).toBe('1')
  })

  it('is numeric on a phone keyboard', () => {
    expect(render().props.inputMode).toBe('numeric')
  })
})

describe('normalise', () => {
  it('floors at the minimum and refuses nonsense', () => {
    expect(normalise('4')).toBe(4)
    expect(normalise('0')).toBe(1)
    expect(normalise('-3')).toBe(1)
    expect(normalise('')).toBe(1)
    expect(normalise('abc')).toBe(1)
  })

  it('honours a caller minimum', () => {
    expect(normalise('1', 2)).toBe(2)
  })
})
