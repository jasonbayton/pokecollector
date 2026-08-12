// The card search pages its results with the left and right arrow keys. Every
// surface that can sit over the page has to suspend them, or the page turns
// underneath whatever the user is actually reading or filling in.
//
// Two of these belong to the page (its card dialog, its filter sheet, its own
// manual-card modal) and two are opened over it by the global quick-add control
// (the scanner and the same manual-card modal). The page cannot see the latter
// two on its own, which is why they are named here rather than assumed.
export function cardSearchKeysSuspended({
  cardDialogOpen = false,
  filtersOpen = false,
  pageCustomCardOpen = false,
  scannerOpen = false,
  quickAddCustomCardOpen = false,
} = {}) {
  return Boolean(
    cardDialogOpen
    || filtersOpen
    || pageCustomCardOpen
    || scannerOpen
    || quickAddCustomCardOpen,
  )
}
