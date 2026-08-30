import { describe, expect, it } from 'vitest'

import { scannerHighResolutionEnabled } from './scannerSettings'

describe('scannerHighResolutionEnabled', () => {
  it('is off for an absent or false installation setting and leaves unrelated settings alone', () => {
    expect(scannerHighResolutionEnabled({ tcgdex_digital_sets_enabled: 'true' })).toBe(false)
    expect(scannerHighResolutionEnabled({ scanner_high_resolution: 'false' })).toBe(false)
  })

  it('is on only for the persisted true value', () => {
    expect(scannerHighResolutionEnabled({ scanner_high_resolution: 'true' })).toBe(true)
  })
})
