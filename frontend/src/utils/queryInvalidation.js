export function invalidateTcgdexFilterLanguages(queryClient) {
  queryClient.invalidateQueries({ queryKey: ['tcgdex-filter-languages'] })
  // Collection and wishlist mutations can change species completion and the
  // ordering shown on Pokédex detail pages.
  queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === 'pokedex' })
}

/** Refresh every cached card-tile view affected by a collection/wishlist mutation. */
export function invalidateCardState(queryClient, { setId } = {}) {
  queryClient.invalidateQueries({ queryKey: ['collection'] })
  // The bounded picker lookups are the same data under different keys. Without
  // these, a trade or binder picker goes on offering a card the mutation just
  // removed, and the set and variant dropdowns keep listing sets no longer
  // owned.
  queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === 'collection-search' })
  queryClient.invalidateQueries({ queryKey: ['collection-facets'] })
  queryClient.invalidateQueries({ queryKey: ['wishlist'] })
  queryClient.invalidateQueries({ queryKey: ['dashboard'] })
  queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === 'card-search' })
  queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === 'pokedex' })
  if (setId) {
    queryClient.invalidateQueries({ queryKey: ['set-checklist', setId] })
  } else {
    queryClient.invalidateQueries({ predicate: (query) => query.queryKey[0] === 'set-checklist' })
  }
}

/** Refresh every owner-only surface whose payload carries has_scan_photo. */
export function invalidateCollectionPhotoState(queryClient) {
  // Blob queries use staleTime Infinity. Remove them so replacements/deletions
  // cannot keep rendering a stale object after a failed 404 refetch.
  queryClient.removeQueries({ queryKey: ['collection-photo'] })
  invalidateCardState(queryClient)
  const affected = new Set([
    'duplicates',
    'binder-cards',
    'binder-entry-equivalents',
    'binder-print-optimization',
  ])
  queryClient.invalidateQueries({ predicate: (query) => affected.has(query.queryKey[0]) })
}
