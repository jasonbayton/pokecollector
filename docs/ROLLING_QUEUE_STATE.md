# Rolling scan queue: where this was parked

Parked 2026-08-13 so the upstream v1.38.0 merge could go first.

Branch: `feat/rolling-scan-queue`, pushed to the fork. **Not tagged**, so the
container's deploy timer cannot pick it up: it selects on `bayton-v*` tags only.

## What is built

The backend half, in one commit.

- `POST /recognize/jobs/{job_id}/items` appends photos to an open job. This is
  what the shutter is meant to call.
- `POST /recognize/jobs` gained a `rolling` form flag, which opens the session
  with the first photo.
- `COMPOSITE_LINGER_SECONDS = 8` holds each appended photo briefly.
- `COMPOSITE_GROUP_SIZE = 4` releases a full group early, so a fast shooter
  never waits.
- `MAX_PENDING_ITEMS = 50` bounds recognition in flight across all of a user's
  jobs. Resolved items do not count, because their photo is already deleted.
- 8 tests in `backend/tests/test_rolling_scan_queue.py`. 921 backend tests pass.

## Why the linger exists

This is the part to re-read before changing anything. The composite processor
tiles two to four cards into ONE vision request. A photo that became claimable
the instant it arrived would always be recognised alone, and the scan bill would
roughly quadruple. The linger is what makes "photographing is submitting"
affordable rather than four times the price.

Mutation-verified: setting the linger to 0 fails the held-photo test, and
disabling the early release fails only the full-group test.

## What is NOT built

The entire frontend. Nothing calls these endpoints, so the app behaves exactly
as it did: staging tray, Start scanning button, no capture-while-scanning.

Remaining, roughly in order:

1. Capture posts immediately: open a rolling job on the first shot, append after.
2. Remove the staging tray, the 0/50 counter, the submit button, and the
   "scan individually" toggle.
3. Gate the shutter on the 50-pending ceiling.
4. Roll over to a fresh job when one fills at `MAX_FILES_PER_JOB`. Intended
   client side, because a POST to job A silently landing in job B is
   surprising. Not yet decided with Jason.

## Open questions for Jason

- **8 seconds is a judgement, not a measurement.** It should be checked against
  how fast someone actually photographs a stack before the UI is built on it.
- Whether job rollover at 50 photos belongs client side or server side.

## Expect a rebase

This touches `services/scan_storage.py` and `api/scan_jobs.py`, and the frontend
work will touch the scanner components. All are likely to move in the v1.38.0
merge, so rebase onto `local-deployment` before resuming rather than merging.
