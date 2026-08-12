// The card search pages its results with the left and right arrow keys. Every
// surface that can sit over the page has to suspend them, or the page turns
// underneath whatever the user is actually reading or filling in.
//
// Two of these belong to the page (its card dialog, its filter sheet, its own
// manual-card modal) and three are opened over it by the global quick-add
// control (the scanner, the same manual-card modal, and the quick-add menu
// itself). The page cannot see those on its own, which is why they are named
// here rather than assumed. The bare menu counts: it dims the page and takes
// focus, so paging the results underneath it is the same defect as paging them
// under a dialog.
export function cardSearchKeysSuspended({
  cardDialogOpen = false,
  filtersOpen = false,
  pageCustomCardOpen = false,
  scannerOpen = false,
  quickAddCustomCardOpen = false,
  quickAddMenuOpen = false,
} = {}) {
  return Boolean(
    cardDialogOpen
    || filtersOpen
    || pageCustomCardOpen
    || scannerOpen
    || quickAddCustomCardOpen
    || quickAddMenuOpen,
  )
}
