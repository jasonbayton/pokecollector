import { describe, expect, it } from 'vitest'

import { cardNumberMatches } from './cardNumbers'

describe('cardNumberMatches', () => {
  it('matches padded collector numbers but keeps suffixes distinct', () => {
    expect(cardNumberMatches('044', '44')).toBe(true)
    expect(cardNumberMatches('74a', '74a')).toBe(true)
    expect(cardNumberMatches('74a', '74')).toBe(false)
  })
})
