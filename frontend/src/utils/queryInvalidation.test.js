import { describe, expect, it, vi } from 'vitest'
import { invalidateCardState } from './queryInvalidation'

describe('invalidateCardState', () => {
  it('refreshes card tile views and the active set checklist without global invalidation', () => {
    const invalidateQueries = vi.fn()
    invalidateCardState({ invalidateQueries }, { setId: 'sv1_en' })
    expect(invalidateQueries).toHaveBeenCalledTimes(8)
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['set-checklist', 'sv1_en'] })

    // The picker lookups are the same data under different keys. Leaving them
    // out meant a trade picker went on offering a card the mutation had just
    // removed. Asserted by key rather than only by count, so the count staying
    // right for the wrong reason would still fail.
    expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: ['collection-facets'] })
    const searchCall = invalidateQueries.mock.calls.find(
      ([filters]) => typeof filters.predicate === 'function'
        && filters.predicate({ queryKey: ['collection-search', 'trades', ''] })
    )
    expect(searchCall).toBeTruthy()
  })

  it('refreshes every cached set checklist when the mutation has no set context', () => {
    const invalidateQueries = vi.fn()
    invalidateCardState({ invalidateQueries })
    expect(invalidateQueries).toHaveBeenCalledTimes(8)

    const checklistCall = invalidateQueries.mock.calls.find(
      ([filters]) => typeof filters.predicate === 'function'
        && filters.predicate({ queryKey: ['set-checklist', 'sv1_en'] })
    )
    expect(checklistCall).toBeTruthy()
    expect(checklistCall[0].predicate({ queryKey: ['card-search'] })).toBe(false)
  })
})
