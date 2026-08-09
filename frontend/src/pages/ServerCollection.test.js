import { describe, expect, it } from 'vitest'

import { entryPrice } from './ServerCollection'

const card = {
  price_trend: 0,
  price_market: 4,
  price_market_holo: 9,
  price_trend_holo: 11,
}

describe('entryPrice', () => {
  it('ignores a zero trend rather than treating it as a real price', () => {
    // The hand-rolled `price_trend ?? price_market` it replaced returned 0 here,
    // so a card priced at 4 was matched by a "under 1" range.
    expect(entryPrice({ card, variants: [] }, 'price_trend')).toBe(4)
  })

  it('uses the dearest printing anyone holds', () => {
    // A shared row can span several people's printings.
    expect(entryPrice({ card, variants: ['Normal', 'Reverse Holo'] }, 'price_trend')).toBe(11)
  })

  it('falls back to the plain price when nobody records a printing', () => {
    expect(entryPrice({ card, variants: undefined }, 'price_trend')).toBe(4)
  })

  it('is zero when a card has no usable price at all', () => {
    expect(entryPrice({ card: {}, variants: [] }, 'price_trend')).toBe(0)
  })
})
