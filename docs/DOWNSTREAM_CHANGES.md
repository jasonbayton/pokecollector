# Downstream changes in this fork

A description of everything this fork adds on top of upstream, written for the
upstream maintainer deciding what is worth pulling in.

**Baseline:** upstream `v1.37.0`. **Branch that runs:** `local-deployment`.

At the time of writing that is 133 non-merge commits, 77 new files and 101
modified files, adding 10 API endpoints, 3 database tables and roughly 470
tests over the upstream suite.

Nothing here has been submitted upstream. Some of it is deliberately local and
should probably stay that way; that is called out per item below. Each section
ends with an honest assessment of how cleanly it would extract, because a
feature that took a fork-only dependency is more work to accept than the diff
suggests.

## How to read the "extractability" notes

- **Clean** - the feature's files all exist upstream or are new, it takes no
  fork-only dependency, and it could be cherry-picked or re-implemented from
  the description without archaeology.
- **Entangled** - it works, but it leans on something else in this list.
  Taking it means taking that too, or reworking it.
- **Local by design** - it encodes a decision specific to this deployment.
  Documented for completeness, not offered.

Verify any claim here with `git log v1.37.0..local-deployment -- <path>`. Where
a statement is unverified, it says so.

---

# 1. Security fixes

These are the items worth reviewing first, because they are defects in upstream
rather than additions to it.

## 1.1 Forgeable sessions when the JWT secret is empty

**Branch:** `fix/jwt-empty-secret-forgeable`

If the configured JWT secret was empty, tokens were still signed and verified
against that empty string, so anyone could mint a valid session for any user.
The fix generates and persists a secret when none is configured rather than
proceeding with an empty one.

**Files:** `backend/services/auth.py`, `backend/api/auth.py`

**Extractability: clean.** This is a straight bug fix and does not depend on
anything else in this fork. If you take one thing from this document, take
this.

## 1.2 Multi-user mode lockout

**Branch:** `fix/multi-user-mode-lockout`

A deployment could get into a state where multi-user mode was enabled with no
usable administrator, locking every route behind an account nobody held.
Includes `backend/scripts/set_admin_password.py` as the recovery path.

**Files:** `backend/api/auth.py`, `backend/services/auth.py`,
`backend/scripts/set_admin_password.py`

**Extractability: clean.**

## 1.3 Settings endpoints returned usable credentials

Bulk settings endpoints returned stored API keys in plaintext to any caller who
could read settings. No endpoint now returns a usable key; the UI is given
presence and masking only.

**Files:** `backend/api/settings.py`, `backend/services/scanner_key_sharing.py`

**Extractability: clean**, though it is easier to take alongside section 2,
since the provider abstraction is what introduces per-user keys.

---

# 2. Scanner provider abstraction

Upstream hardcodes Gemini. This fork puts a provider interface in front of it
so an OpenAI-compatible endpoint can be used instead.

**New files:** `backend/services/scan_providers.py`,
`backend/services/scanner_key_sharing.py`

**New configuration:** `SCANNER_PROVIDER` (default `gemini`, so the upstream
default is unchanged), `OPENAI_API_KEY`, `OPENAI_MODEL`.

Two decisions worth knowing:

- **Shared environment keys are opt-in and default off.** A server-wide key
  being silently available to every user on a multi-user install is a billing
  and privacy surprise, so a user supplies their own key unless the operator
  explicitly enables sharing.
- **`scanner_provider` is validated**, so an unknown value fails loudly at
  configuration time rather than at the first scan.

**Extractability: clean.** The interface is additive and the Gemini path
remains the default.

---

# 3. Scan queue and review inbox

The largest body of work, and the one with the most fork-specific opinion in
it. Upstream scans synchronously; this fork queues scans, processes them in the
background under leases, and reviews the results in an inbox.

Most of this is upstream's own architecture as of `v1.37.0` and was merged
forward, so the descriptions below are the *downstream additions on top of it*.

## 3.1 Add all confident

**Endpoint:** `POST /recognize/jobs/{job_id}/add-all-confident`
**New file:** `backend/services/scan_bulk_add.py`

Files every item the matcher was confident about in one action, atomically,
under PostgreSQL row locks so a concurrent review cannot double-add.

Depends on persisted match confidence (3.2).

**Extractability: entangled** with 3.2.

## 3.2 Persisted match confidence

**Columns added to `scan_job_items`:** `identity_confident`,
`identity_decision`, `suggested_match_id`

All three are deliberately nullable. Null means *no verdict was ever recorded*,
which is not the same as the matcher having been unsure, and the review UI
needs that difference to stay honest about what it does and does not know.
Rows written before the columns existed must read as unknown, never as "not
confident" and never as confident.

`suggested_match_id` holds a `matches[].id` such as `base1-4_en`, never a
`matches[].tcg_card_id`. The same card can appear once per language in one
candidate list, so the card id does not identify a candidate and badging on it
marks every language copy at once.

**Extractability: clean**, but it needs a migration.

## 3.3 Composite matching concurrency

The composite processor tiles 2-4 cards into a single vision request as a cost
optimisation. Downstream bounds the concurrent TCGdex requests it makes when
resolving the positions afterwards, so a single composite cannot open more
connections than the catalogue tolerates.

**Files:** `backend/services/scan_queue.py`

**Extractability: clean.**

## 3.4 Per-item photo re-take

**Endpoint:** `POST /recognize/jobs/{job_id}/items/{item_id}/photo`

Upstream's `retry` re-queues the *same stored bytes*. That answers a provider
timeout and cannot answer glare or a bad crop, so a bad photo meant discarding
the whole job. Worse, retry was only offered for `failed` items and `done`
items with no matches, so a photo that produced a confident but *wrong* match
had no route at all except dismissal.

The re-take endpoint replaces one unresolved item's photo and re-queues it
individually. It shares the reset with retry via `_reset_item_for_rescan`, but
deliberately does **not** inherit retry's requirement that the old file still
exist: supplying replacement bytes is precisely the case where it may not.

Three details that took work and would be easy to get wrong on a
re-implementation:

- **The old file is deleted only after the commit**, and a failed commit
  deletes the *new* orphan instead, so the row and its file are never parted in
  either direction.
- **The review panel keyed its photo fetch on the item id**, so a re-take left
  it showing the photo just replaced while the scan ran on the new one. The
  payload now carries `image_token`, hashed from the stored path, which changes
  when the file changes and not when the status advances.
- **The panel treats an item as busy from the moment a re-take is submitted**,
  not when the server reports it. `ScanAddModal` writes to the collection
  *before* the scan is resolved, so acting on a stale candidate in that window
  filed a card nothing had matched and then failed to resolve with a 409,
  leaving it in the collection with no prompt.

**Files:** `backend/api/scan_jobs.py`, `backend/services/scan_queue.py`,
`frontend/src/components/ScanReview.jsx`, `frontend/src/pages/ScanQueue.jsx`

**Extractability: clean.** No schema change. `image_token` is additive.

## 3.5 Live viewfinder

**Branch:** `live-viewfinder-batch-capture`
**New files:** `frontend/src/components/LiveCardViewfinder.jsx`,
`frontend/src/utils/cameraCapture.js`

An in-app `getUserMedia` viewfinder that stages one card per tap, beside
upstream's `<input capture>` route rather than replacing it. The file inputs
remain the fallback for every browser, permission state and device the
viewfinder cannot serve.

Worth knowing if you adopt it:

- **A refusal is recoverable.** The denial message tells the user to allow the
  camera in browser settings; the session must therefore let them try again
  afterwards, or the only way back in is closing the whole scanner. A browser
  that is still blocking rejects immediately without re-prompting, so this
  cannot nag. An earlier version blocked all further attempts for the session
  and contradicted its own message.
- **It needs a secure context**, so it is unavailable over plain HTTP. The
  error copy says so specifically rather than failing vaguely.
- **The session must be dropped, not merely disposed, on cleanup.** A disposed
  session refuses `start()` silently, and the ref outlives the cleanup.

**Extractability: clean**, but it is a meaningful surface with real device
behaviour behind it. See "Testing" below for what is and is not verified.

## 3.6 Quick add and scanner presentation

The scanner is reachable from the nav, and the two camera routes are labelled
so they read as different actions: the in-app viewfinder versus "Use camera
app", grouped under "Other ways to add". A job page offers "Scan more cards",
because a job's photo list is fixed once queued and carrying on means a second
job.

**Extractability: local by design.** This is presentation opinion, not
mechanism. Described so the reasoning is available, not offered as a patch.

---

# 4. Public sharing

## 4.1 Public collection

**Endpoint:** `GET /profiles/{handle}/collection`
**Column:** `users.public_show_collection` (default `false`)
**New file:** `backend/services/public_profile.py`

Opt-in, default off. The payload deliberately carries no purchase price,
condition or grade, and the page module imports no mutating call, so it is
read-only by construction rather than by discipline.

**Extractability: clean.** Needs a migration for one boolean.

## 4.2 Public collection card zoom

**New file:** `frontend/src/components/CardImageDialog.jsx`

Tap a card to see the artwork at reading size, built on the shared `Modal`.

One detail that cost three deploys: the box is `aspect-[8/11]`, **not** the
`2.5/3.5` of a physical card. TCGdex artwork is 600x825 because the scans carry
bleed, so sizing to a real card leaves a band above and below that the
surrounding frame turns into an apparent crop. It is also width-driven, because
a fixed height stops matching the artwork once a narrow phone clamps the width.

**Extractability: clean.**

## 4.3 Server-wide social view

**Endpoint:** `GET /social/server`
**Files:** `backend/api/social.py`, `frontend/src/pages/` server view

An aggregate across every user who has opted in.

**Extractability: entangled** with 4.1, since it aggregates the same opt-in.

---

# 5. Collection features

## 5.1 Recycle bin

**Endpoints:** `GET /deleted/`, `POST /deleted/{entry_id}/restore`
**Table:** `deleted_collection_items`
**New file:** `backend/services/deleted_collection.py`

Deletions are recorded with enough context to restore them, including who
performed them. Cards removed by hand can be put back; trades and sales are
deliberately not listed, because they are not deletions.

**Extractability: clean.** Needs a migration for the new table.

## 5.2 Binder slots and layout

**Endpoints:** `GET/POST/PUT/DELETE /{binder_id}/layout[/slots]`
**Table:** `binder_slots`
**Columns:** `grid_rows`, `grid_columns` on binders
**New files:** `backend/services/binder_slots.py`,
`backend/services/binder_layout.py`, `backend/services/binder_csv.py`

Explicit page and pocket placement within a binder, with concurrency handling
so two placements into the same pocket cannot both succeed (one gets a 409).

**Extractability: clean**, and the largest single migration in this list.

## 5.3 Condition and grade

**Columns:** `condition` (Mint/NM/LP/MP/HP) and `grade` (default `raw`) on
collection items.

**Extractability: clean.**

## 5.4 Quantity limits

**New file:** `backend/services/quantity_limits.py`

**Extractability: clean.** Unverified whether this has any fork-specific
policy baked in; check before adopting.

---

# 6. Currency

**New file:** `backend/services/exchange_rates.py`
**Configuration:** `DEFAULT_CURRENCY` (default `EUR`, so upstream behaviour is
unchanged)

GBP is genuinely converted, not relabelled, and the conversion reaches the
Telegram price alerts as well as the UI. Relabelling was the first
implementation and was rejected; the tests assert conversion specifically.

**Extractability: clean.**

---

# 7. Testing

The suite is substantially larger than upstream's: 904 backend tests
(63 skipped) and 455 frontend tests on `local-deployment`.

Two things about the frontend harness that will bite anyone porting tests:

- **vitest runs in the `node` environment.** There is no jsdom and no
  testing-library. Component tests use `renderToStaticMarkup` from
  `react-dom/server` plus `vi.mock`, and some suites wrap the JSX runtime to
  capture the element tree because there is no DOM to query. **Effects do not
  run**, so anything living in `useEffect` cannot be tested this way without a
  React mock.
- **`scripts/check-translations.mjs` is one-directional.** It errors only on
  keys referenced in code but missing from `en.js`. It cannot see hardcoded JSX
  literals, orphaned keys, or keys missing from non-English locales. It passed
  green for a long time while the entire viewfinder surface was English-only in
  `de` and `fr`. Do not read a pass as translation coverage.

All 107 `scanner.*` keys now exist in `en`, `de` and `fr`, with placeholders
verified identical across locales.

## What is not verified

Stated plainly so nobody inherits a false assurance:

- **The re-take flow has not been exercised on real hardware.** The backend
  path, the guards, the commit-failure ordering and the UI gating are all
  covered by tests and were mutation-verified, but the end-to-end capture on a
  physical phone against a deployed instance has not been performed.
- The live viewfinder itself *was* verified on a physical Pixel earlier in its
  development.

---

# 8. Suggested order if you want any of it

1. **Section 1** - the security fixes, in full. They are defects, they are
   self-contained, and 1.1 is the serious one.
2. **Section 2** - the provider abstraction, if multi-provider is interesting.
   Additive and default-preserving.
3. **3.4** - per-item re-take. Small, no migration, and closes a real gap:
   today a confidently-wrong match can only be dismissed.
4. **4.2** - the card zoom dialog. Small and self-contained.
5. Everything else needs a migration and encodes more opinion.
