# Issue 7 plan - make opening a product one continuous session

## Scope and evidence

This is a build plan, not an implementation. It is grounded in the current
`feat/product-open-session` checkout at `bfdfd41`, rather than assuming the
issue description remains exact.

The relevant behaviour found in this checkout is:

- `ScanJob` and `ScanJobItem` in `backend/models.py` are a persistent,
  user-owned, expiring review queue. A job currently has no product reference.
  Deleting a job cascades its items.
- `backend/api/scan_jobs.py:enqueue_scan_job` delegates job and image creation
  to `services.scan_storage.create_scan_job`; its payload only carries files
  and individual-position choices. `job_progress` in
  `backend/services/scan_queue.py` is the common list and detail payload.
- `backend/services/scan_bulk_add.py:add_all_confident_scan_items` deliberately
  performs catalogue preparation before it locks `ScanJob` and eligible
  `ScanJobItem` rows. Its docstring explains why: `ensure_card_exists` can
  fetch and commit, and an upstream delay must not hold review locks. The
  locked phase rechecks the stored candidate before it changes collection or
  review state, commits once, then deletes images after commit.
- `_add_collection_copy` currently locks an existing merge row, increments it
  or inserts a new `CollectionItem`, but returns no source row for provenance.
  It always uses `Mint`, and derives a card-level default variant.
- `backend/api/products.py:_link_collection_items` is the existing exact-link
  implementation. It locks product-selected collection rows by ascending id,
  checks unlinked capacity, then creates or extends `ProductCard` rows and
  marks the product opened. `ProductCard.collection_item_id` is intentionally
  not a foreign key. Its history must survive the collection row being deleted
  after a sale; `initial_quantity`, `active_quantity`, and `sold_quantity`
  carry that history.
- The retrospective picker in `frontend/src/pages/Products.jsx` filters
  collection rows by `added_at`. This cannot find a duplicate that was merged
  into an older collection row, because the collection merge does not refresh
  `added_at`.
- `backend/services/rapid_set_entry.py:commit_rapid_set_entry` is the required
  transaction precedent. It releases an earlier read transaction, revalidates
  prepared data, locks collection merge rows in one sorted order, and commits
  once. Its comment explains the deadlock avoided by not following browser
  order.
- `GET /api/products/{product_id}` already returns the product's linked cards,
  live values, realised gains, calculated current value, and P and L through
  `_refresh_product_response`. This is sufficient for a product-level recap.

The library page `library/projects/pokecollector.md` was read only as
low-confidence generated navigation material. Source in this checkout and the
two open GitHub issues are the basis for this plan.

## Decisions

### Session model and schema

Add the following columns to `scan_jobs` in `backend/models.py` and to the
startup migration list in `backend/database.py:_run_migrations`.

| Column | ORM definition | Existing databases | Fresh database |
| --- | --- | --- | --- |
| `product_id` | nullable `ForeignKey("product_purchases.id", ondelete="SET NULL")`, indexed | `ALTER TABLE scan_jobs ADD COLUMN IF NOT EXISTS product_id INTEGER REFERENCES product_purchases(id) ON DELETE SET NULL`, then add an index | `create_all` creates the nullable foreign key and index from the model |
| `default_condition` | non-null string, ORM default and `server_default="Mint"` | `ALTER TABLE scan_jobs ADD COLUMN IF NOT EXISTS default_condition VARCHAR NOT NULL DEFAULT 'Mint'` | `create_all` supplies the database default, not merely the ORM default |
| `default_lang` | non-null string, ORM default and `server_default="en"` | `ALTER TABLE scan_jobs ADD COLUMN IF NOT EXISTS default_lang VARCHAR NOT NULL DEFAULT 'en'` | `create_all` supplies the database default, not merely the ORM default |

The database defaults are compatibility defaults for old rows and the ordinary
scanner. The new product-opening request must send both values explicitly and
the API must validate them with the existing condition and TCGdex-language
validators before it creates a job. This makes the batch choice user supplied
for the new workflow without inventing a stated-versus-guessed attribute
model. Variant remains an item-level review decision using the application's
existing controls, not a new batch attribute model.

Use a real foreign key for `scan_jobs.product_id`, unlike
`ProductCard.collection_item_id`: a job is a temporary workflow container, so
it has no historical value once its product has been deleted. `ON DELETE SET
NULL` deliberately preserves the review job and its images as an ordinary,
unlinked scan job instead of deleting the user's unfinished work. Do not add a
foreign key from `ProductCard` to `CollectionItem`.

The migration test must cover both paths. A new schema made through
`Base.metadata.create_all()` must accept an insert that omits both default
columns and store `Mint` and `en`. A pre-feature schema upgraded by
`_run_migrations` must show the same columns, defaults, foreign-key delete
action, and index. Running `create_all` before migrations is specifically why
the model has `server_default` as well as the migration's `DEFAULT` clause.

### API and queue plumbing

1. Add a product-opening entry point in `backend/api/products.py`, for example
   `POST /api/products/{product_id}/open-scan`, with a small request schema in
   `backend/schemas.py` for `condition` and `lang`. It locks the owned product,
   rejects a sealed-product sale and returns the verified product id and chosen
   defaults for the authenticated scanner request. `enqueue_scan_job` repeats
   the ownership and lifecycle validation before persisting them, rather than
   accepting a product id blindly from the browser.
   This explicit transition records the real act of opening even if the
   customer later abandons review. This is deliberately recoverable: the
   existing `_validate_lifecycle_choice` permits an opened product to be sealed
   again while it has no linked-card or ledger history. An accidental open
   which files nothing can therefore be corrected by the user.
2. Extend `enqueue_scan_job` and `create_scan_job` to accept the verified
   product-session values and persist `product_id`, `default_condition`, and
   `default_lang` with the job and items in the existing job-creation
   transaction. Keep the ordinary scanner route and its current request shape
   working when no product values are supplied.
3. Extend `job_progress` and the job-detail payload with the product id and
   batch defaults, plus a minimal product name only when it can be loaded for
   the same user. Do not expose another user's product by joining on an
   unverified id.
4. In `frontend/src/pages/Products.jsx`, add an Open and scan action from a
   sealed or review product. It asks once for condition and language, starts
   the existing `UnifiedCardScanner` with the returned product values, and
   navigates to the normal queue detail after upload. Add product parameters to
   `frontend/src/api/client.js:enqueueScanJob` and thread it through
   `frontend/src/components/UnifiedCardScanner.jsx` and the scanner context.
   The global scanner remains an unlinked scan.

Do not change the scanner worker, leases, fair dispatch, compositing, expiry
period, or image-storage layout. Product ownership is job metadata, not queue
priority.

### Atomic filing and provenance

Extract the reusable, non-HTTP product-link mutation from
`backend/api/products.py:_link_collection_items` into a service, proposed as
`backend/services/product_card_links.py`. The existing product routes call the
service unchanged in behaviour. Give the service a second internal entry point
for copies just created or incremented by a scan: it accepts already locked
collection rows and quantities aggregated by collection-item id, validates
capacity, merges the matching active `ProductCard` when safe, or creates one
with `initial_quantity == active_quantity == filed quantity` and
`sold_quantity == 0`. It must retain the deliberate non-FK source id and set
the product lifecycle to opened.

Refactor `backend/services/scan_bulk_add.py` so its collection helper accepts
the selected condition, variant, and language and returns the locked or newly
flushed `CollectionItem`. It must not commit. Before the locking phase, prepare
every selected catalogue card in the requested job language. After preparation,
release the read transaction exactly as the current bulk path does.

One filing transaction has this fixed order. It is the same order for an
ordinary job, which simply skips the product step.

0. Read the job's `product_id` WITHOUT holding a lock, purely to learn whether
   a product is involved.
1. Lock that `ProductPurchase` by id and user id FIRST, when there is one.
   Locking the job first would deadlock against product deletion: filing would
   hold the job and wait for the product, while deletion holds the product and
   then waits to apply its `SET NULL` to the job.
2. Lock `ScanJob` by id and user id, which serialises duplicate submissions of
   the same job, then revalidate the association read in step 0. Reject the
   request if the product was deleted or sold while preparation was running.

   The request must carry an `expected_product_id` and it must be compared
   against the locked job. Once a product deletion has committed,
   `job.product_id` is null and is indistinguishable from a job that was never
   product-owned, so without that token the common endpoint would silently file
   the card with no provenance instead of returning the conflict this design
   promises.
3. Lock eligible `ScanJobItem` rows by ascending `position`, then revalidate
   their stored suggested candidate and prepared catalogue membership.
4. Combine candidate copies by the full collection merge identity, sort those
   identities lexically, and lock any existing `CollectionItem` targets in that
   exact order. Create and flush missing targets before making product links.
5. Aggregate the filed quantities by returned collection-item id, apply the
   `ProductCard` mutations, mark scan items resolved and clear their image
   paths, then commit exactly once.

This order is intentionally independent of photo order for collection locks,
like rapid set entry. The job and product locks are acquired first because they
are the session's duplicate and lifecycle boundaries. The code must use the
same order for bulk confident filing and one-by-one filing.

Catalogue fetches, language-card creation, and image deletion stay outside the
critical section. Preparation can commit. A rollback leaves the scan item
unresolved and its image path present. Only after the successful commit may
`delete_scan_image` remove the physical file.

Add one explicit atomic review filing endpoint rather than overloading the
existing `resolve` endpoint. For example,
`POST /recognize/jobs/{job_id}/items/{item_id}/file` accepts the selected card,
condition, variant, language, quantity, and optional price. It prepares the
catalogue before locks and then uses the same atomic service. When the job has
no product, that transaction files the collection copy and resolves the item,
with no product work. When it has a product, the same transaction also locks
the product in the stated order and creates or extends its `ProductCard`.
The existing `resolve` endpoint remains the dismiss action and does not add a
collection copy. Update `ScanAddModal` and `ScanQueue` for every job so that
their Add action calls this endpoint.

This endpoint serves ordinary jobs as well as product jobs because the current
`addToCollection` followed by `resolve` sequence can leave a collection copy
behind when resolution fails. A product-only atomic route would retain the
currently-shipping ordinary-job failure and introduce two filing paths that
would need to remain identical in their failure semantics. The product lock is
therefore an optional participant in one transaction, not the endpoint's
reason for existing.

When two items in one product session merge into the same existing collection
row, create or extend one `ProductCard` for their total. Its source id points
to that older collection row, which is precisely why retrospective filtering by
`added_at` is not used.

### Product deletion and unfinished jobs

- Creating a product scan marks the product opened immediately. If the scan is
  abandoned half-reviewed, completed filing remains durable and linked, while
  unresolved photos and items stay reviewable until the existing discard or
  14-day expiry path removes them. The product remains opened, accurately
  reflecting that it was unwrapped.
- The existing product delete route already refuses products with `ProductCard`
  or ledger history. An empty opened product with an unfiled scan may still be
  deleted. The database sets `ScanJob.product_id` to null. The queued work
  survives and is thereafter handled as an unlinked scan; attempts to use the
  stale product filing action receive a conflict and must refresh.
- Product deletion must not delete a scan job, its items, or files. Job
  deletion and expiry retain their present cascade plus post-commit directory
  cleanup.
- Do not attempt to reconstruct deleted product provenance from scan data. A
  product with filed provenance cannot be deleted under the current product
  rules, while an unfiled job has no provenance to preserve.

### Recap

On the final successful file, or when the user chooses View recap, navigate to
the existing product detail and refetch `GET /api/products/{product_id}`. Render
a focused opened-product recap in `frontend/src/pages/Products.jsx` using the
existing `ProductPurchaseResponse` fields: product purchase price, computed
current value, P and L, live linked value, realised gains, and `product_cards`.

No new recap table or query is required. The existing response already loads
`ProductCard.card` and ledger entries and computes the values in the product
service. The recap is deliberately product-level, not a claim that it contains
only the latest scan job: a product may have manually linked cards or multiple
scan sessions. Label it as the product recap and show filed versus unresolved
counts from the current job separately while that job still exists.

## Delivery sequence

Each slice below is complete and releasable on its own. No slice fakes
provenance or relies on retrospective matching.

1. **Backend ownership and lifecycle slice.** Add schema, migration, product
   opening intent, validated job creation, payload fields, deletion semantics,
   and migration/API tests. A product can be opened and a linked job can be
   created, but filing remains the current behaviour until the next slice.
2. **Atomic filing slice.** Extract the product-link service, implement the
   common fixed-lock filing path for confident bulk and for individual review
   of EVERY job, product-owned or not, and add real PostgreSQL concurrency and rollback tests. Ship only
   once every successful product-job file creates or extends `ProductCard` in
   the same commit as its collection copy and scan resolution.
3. **Continuous frontend slice.** Add the product Open and scan entry, batch
   condition and language prompt, the review Add action for every job, progress
   context, and product recap navigation. Review must initialise its condition
   and language from the job's batch defaults while still allowing an
   item-level correction. Preserve all ordinary scanner behaviour other than
   the move to the atomic endpoint.
4. **Polish after usage.** Improve recap presentation and translations based on
   real family use. Do not expand database scope before evidence shows that the
   product-level recap is insufficient.

## Verification plan

Run all backend tests from `backend` with the configured throwaway PostgreSQL
database, then run the focused frontend Vitest tests. New tests must include
negative controls and bystanders, and must be observed failing against a
temporary local mutation before the implementation is accepted.

- Migration tests: fresh `create_all` and upgraded schema agree on defaults,
  nullable association, `ON DELETE SET NULL`, and index. Restart the
  application migration path twice to prove a later startup does not undo it.
- API tests: reject another user's product id, invalid condition or language,
  sold product, expired or deleted product association, and stale product
  values. Prove an unlinked scanner still creates an ordinary job.
- Filing unit tests in `backend/tests/test_scan_bulk_add.py`: new collection
  row and existing older merge row both create correct product provenance;
  mixed product and non-product jobs leave an unrelated collection and
  `ProductCard` untouched; a candidate changing after preparation rolls back
  collection, product, resolution flag, and image-path mutation together.
- PostgreSQL tests, preferably a new per-test-schema test beside
  `backend/tests/test_scan_bulk_add_postgres.py`: pause the first product-job
  filing after the locks, show the second request blocks, then prove exactly
  one collection quantity increment, one product quantity increment, one
  resolution, and no duplicate product card. Start with both an existing row
  and no existing row. The latter is expected to expose issue #17's unresolved
  duplicate-insert race, so do not write a test that claims it is fixed.
- Product lifecycle tests: deleting an empty product nulls a job association
  but retains job/items; deleting after a filed card is still rejected; expiry
  or discard does not alter existing `ProductCard` history.
- Frontend tests around `UnifiedCardScanner`, `ScanQueue`, `ScanAddModal`, and
  `Products`: the product prompt's chosen values reach the API, and BOTH a
  product job and an ordinary job call the atomic file endpoint rather than
  `addToCollection`. An ordinary job retaining its current calls would preserve
  the orphan-card bug this endpoint exists to remove. Recap labels must not
  imply one-job-only history.

Use a unique PostgreSQL schema for each new concurrency test. Do not run tests
against the live family instance, and do not broaden an existing test's
`drop_all` behaviour to a shared database.

## Risks not stated in the issue

- Catalogue preparation can fetch and commit. Holding product or scan locks
  while it waits would turn provider latency into blocked review and deadlock
  risk, hence the prepare, rollback, revalidate shape.
- A job may be deleted, expire, or have its product deleted between preparation
  and filing. The locked re-read must return a conflict without filing a card.
- Multiple photo items can target one old collection row. Provenance quantities
  must be summed rather than creating competing product links for the same
  copies.
- `ProductCard` records a source row without a foreign key because sale history
  outlives that row. Any cleanup or deduplication that treats it as referential
  garbage would corrupt sales history.
- Product deletion, sales, trades, unlinking, and filing all touch overlapping
  inventory. Fixed lock order and real PostgreSQL tests are required because
  SQLite does not validate row-lock behaviour.
- Issue #17 remains open. `FOR UPDATE` protects an existing collection merge
  row but cannot lock an absent one. This feature must neither add a second
  insert path nor describe its tests as closing that race. The larger unique
  merge-identity migration, deduplication, and upsert work belongs to #17.
- Existing product deletion is authorised only by linked-card and ledger
  history. The new `SET NULL` relation makes an unfiled opened session safe to
  retain, but the UI must not cache and reuse a deleted product id.
- Image removal is a filesystem action, not transactional. It remains
  post-commit and must tolerate a failed cleanup without rolling back recorded
  provenance.

## Explicitly out of scope

- Issue #6's attribute-confirmation lattice, an unassessed condition state,
  distinguishing guessed from stated data, and backfilling historical Mint or
  variant values.
- Fixing issue #17, adding a collection merge uniqueness constraint, deduping
  production rows, or converting every collection writer to an upsert.
- Reworking scanner recognition, candidate ranking, variant recognition,
  provider configuration, queue fairness, expiry duration, or photo storage.
- Reconstructing old product provenance from `added_at`, previous scans, or
  collection history.
- Changing the intentional non-FK relationship from `ProductCard` to its
  source collection row, or deleting sales and ledger history with a product.
- A new valuation engine, a new recap persistence table, purchase-cost
  allocation across pulls, realised-profit accounting changes, or a claim that
  the recap isolates one scan job from the whole product history.
- Any migration or test against the live family database.
