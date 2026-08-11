"""How many copies of one card a single row may claim.

Collection rows are uncapped, so the old limit of 99 was inconsistent as well
as low: you could own five hundred of a card and then be unable to record more
than ninety-nine of them in a binder or wishlist.

The number lives here rather than being repeated at each call site, because it
appears in check constraints, request validation, merge arithmetic, CSV import
and user-facing copy, and those had drifted apart once already.
"""

MAX_CARD_QUANTITY = 9999
