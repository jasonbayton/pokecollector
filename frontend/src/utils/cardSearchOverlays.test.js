import { describe, expect, it } from 'vitest'

import { cardSearchKeysSuspended } from './cardSearchOverlays'

describe('cardSearchKeysSuspended', () => {
  it('lets the arrow keys page the results when nothing is over the page', () => {
    expect(cardSearchKeysSuspended()).toBe(false)
    expect(cardSearchKeysSuspended({
      cardDialogOpen: false,
      filtersOpen: false,
      pageCustomCardOpen: false,
      scannerOpen: false,
      quickAddCustomCardOpen: false,
      quickAddMenuOpen: false,
    })).toBe(false)
  })

  it.each([
    'cardDialogOpen',
    'filtersOpen',
    'pageCustomCardOpen',
    'scannerOpen',
    'quickAddCustomCardOpen',
    // The bare menu counts as much as a dialog: it dims the page and holds
    // focus, so paging the results underneath it is the same defect.
    'quickAddMenuOpen',
  ])('suspends them while %s owns the screen', surface => {
    expect(cardSearchKeysSuspended({ [surface]: true })).toBe(true)
  })
})
