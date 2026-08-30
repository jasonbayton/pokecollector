"""Fair, restart-safe background processing for sanitized card scans."""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from models import ScanJob, ScanJobItem, ScanQueueUserState, User
from services.gemini_rate_limit import gemini_priority_scope
from services.scan_storage import (
    ScanItemNoLongerReviewable,
    ScanUploadError,
    delete_job_directory,
    delete_scan_image,
    resolve_scan_path,
    sanitize_image_bytes,
    store_sanitized_image,
)
from services.scan_bulk_add import confident_addable_count

logger = logging.getLogger(__name__)

MAX_RECOGNITION_ATTEMPTS = 3
LEASE_SECONDS = 10 * 60
# How many recognised composite positions may be matched at once. The profitable
# work in a matcher is network, not database: four TCGdex round trips against two
# short synchronous reads. Two positions overlap those waits without multiplying
# the burst aimed at a free public API, and without needing a single extra
# database connection - see default_composite_processor.
COMPOSITE_MATCH_CONCURRENCY = 2
TRANSIENT_BACKOFF_SECONDS = (30, 120, 600, 1800, 3600, 21600)
# Retries of an unreachable card catalogue are bounded, unlike other transient
# failures. Each one re-runs the vision extraction before it can reach the
# catalogue at all, so on a metered provider a multi-day outage would charge
# for the same photo dozens of times without ever being able to succeed.
#
# Four attempts follow the backoff above, so the last one falls about twelve
# minutes after the first: long enough to ride out a blip, short enough that a
# real outage stops spending. The item is then left failed with the reason on
# it, and the user's own retry resets the count.
MAX_CATALOGUE_UNREACHABLE_ATTEMPTS = 4
CATALOGUE_UNREACHABLE_RETRY_REASON = "catalogue_unreachable"
RECOGNITION_BACKOFF_SECONDS = (2, 10, 30)
TERMINAL_ITEM_STATUSES = {"done", "failed"}


@dataclass(frozen=True)
class ClaimedScanItem:
    item_id: int
    lease_token: str
    item_ids: tuple[int, ...] = ()
    composite: bool = False

    @property
    def all_item_ids(self) -> tuple[int, ...]:
        return self.item_ids or (self.item_id,)


class TransientScanError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        retry_reason: str | None = None,
    ):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.retry_reason = retry_reason


class RecognitionScanError(RuntimeError):
    pass


class PermanentScanError(RuntimeError):
    pass


def _eligible_items(now: datetime.datetime):
    return and_(
        ScanJobItem.status.in_(["pending", "retrying"]),
        or_(ScanJobItem.next_attempt_at.is_(None), ScanJobItem.next_attempt_at <= now),
        ScanJob.expires_at > now,
    )


def recover_expired_leases(db: Session, *, now: datetime.datetime | None = None) -> int:
    """Return work abandoned by a crashed worker to the retry queue."""
    now = now or datetime.datetime.utcnow()
    items = (
        db.query(ScanJobItem)
        .filter(
            ScanJobItem.status == "processing",
            ScanJobItem.lease_expires_at.is_not(None),
            ScanJobItem.lease_expires_at <= now,
        )
        .all()
    )
    for item in items:
        item.status = "retrying"
        item.next_attempt_at = now
        item.retry_reason = None
        item.lease_token = None
        item.lease_expires_at = None
        item.error = "Processing was interrupted and will resume automatically."
        item.updated_at = now
    if items:
        db.commit()
    return len(items)


def claim_next_scan_item(
    db: Session,
    *,
    now: datetime.datetime | None = None,
    lease_seconds: int = LEASE_SECONDS,
) -> ClaimedScanItem | None:
    """Atomically claim one individual photo or a two-to-four-photo group."""
    now = now or datetime.datetime.utcnow()

    state = (
        db.query(ScanQueueUserState)
        .join(ScanJobItem, ScanJobItem.user_id == ScanQueueUserState.user_id)
        .join(ScanJob, ScanJob.id == ScanJobItem.job_id)
        .filter(_eligible_items(now))
        .order_by(
            ScanQueueUserState.last_dispatched_at.asc().nullsfirst(),
            ScanQueueUserState.user_id.asc(),
        )
        .with_for_update(skip_locked=True)
        .first()
    )
    if state is None:
        db.rollback()
        return None

    item = (
        db.query(ScanJobItem)
        .join(ScanJob, ScanJob.id == ScanJobItem.job_id)
        .filter(ScanJobItem.user_id == state.user_id, _eligible_items(now))
        .order_by(ScanJob.created_at.asc(), ScanJobItem.position.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if item is None:
        db.rollback()
        return None

    items = [item]
    if item.batch_mode:
        siblings = (
            db.query(ScanJobItem)
            .join(ScanJob, ScanJob.id == ScanJobItem.job_id)
            .filter(
                ScanJobItem.job_id == item.job_id,
                ScanJobItem.position > item.position,
                ScanJobItem.batch_mode.is_(True),
                _eligible_items(now),
            )
            .order_by(ScanJobItem.position.asc())
            .limit(3)
            .with_for_update(skip_locked=True)
            .all()
        )
        items.extend(siblings)

    lease_token = uuid.uuid4().hex
    lease_expires_at = now + datetime.timedelta(seconds=lease_seconds)
    for claimed_item in items:
        claimed_item.status = "processing"
        claimed_item.retry_reason = None
        claimed_item.lease_token = lease_token
        claimed_item.lease_expires_at = lease_expires_at
        claimed_item.updated_at = now
    state.last_dispatched_at = now
    job = item.job
    job.status = "running"
    if not job.started_at:
        job.started_at = now
    job.updated_at = now
    db.commit()
    return ClaimedScanItem(
        item_id=item.id,
        item_ids=tuple(claimed_item.id for claimed_item in items),
        lease_token=lease_token,
        composite=len(items) > 1,
    )


def _backoff(values: tuple[int, ...], failure_count: int) -> int:
    index = max(0, min(len(values) - 1, failure_count - 1))
    return values[index]


def _leased_item(db: Session, claim: ClaimedScanItem) -> ScanJobItem | None:
    return (
        db.query(ScanJobItem)
        .filter(
            ScanJobItem.id == claim.item_id,
            ScanJobItem.status == "processing",
            ScanJobItem.lease_token == claim.lease_token,
        )
        .with_for_update()
        .first()
    )


def _leased_items(db: Session, claim: ClaimedScanItem) -> list[ScanJobItem]:
    return (
        db.query(ScanJobItem)
        .filter(
            ScanJobItem.id.in_(claim.all_item_ids),
            ScanJobItem.status == "processing",
            ScanJobItem.lease_token == claim.lease_token,
        )
        .order_by(ScanJobItem.position.asc())
        .with_for_update()
        .all()
    )


def _refresh_job_status(db: Session, job: ScanJob, now: datetime.datetime) -> None:
    statuses = [row[0] for row in db.query(ScanJobItem.status).filter(ScanJobItem.job_id == job.id).all()]
    if not statuses:
        job.status = "failed"
        job.error_message = "The scan job contains no photos."
        job.finished_at = now
    elif all(status in TERMINAL_ITEM_STATUSES for status in statuses):
        job.status = "done" if any(status == "done" for status in statuses) else "failed"
        job.finished_at = now
    elif any(status == "processing" for status in statuses):
        job.status = "running"
    else:
        job.status = "pending"
    job.updated_at = now


def _apply_identity_outcome(item: ScanJobItem, result: dict) -> None:
    """Record the matcher's verdict alongside the candidates it produced.

    The verdict travels with recognized/matches: written wherever they are
    written, cleared wherever they are cleared. A processor that returns no
    verdict at all (any caller other than the real matcher) counts as not
    confident rather than unknown, because a completed scan was in fact judged.
    The suggested id is kept only while the matcher stands behind it, so an
    unconfident outcome can never leave a stale id for the badge to find.
    """
    confident = bool(result.get("_identity_confident"))
    item.identity_confident = confident
    item.identity_decision = result.get("_identity_decision") or None
    item.suggested_match_id = (
        (result.get("_identity_suggested_match_id") or None) if confident else None
    )


def _clear_identity_outcome(item: ScanJobItem) -> None:
    """Drop a verdict that no longer describes this item's stored candidates."""
    item.identity_confident = None
    item.identity_decision = None
    item.suggested_match_id = None


def complete_claim(db: Session, claim: ClaimedScanItem, result: dict) -> bool:
    now = datetime.datetime.utcnow()
    item = _leased_item(db, claim)
    if item is None:
        db.rollback()
        return False
    item.status = "done"
    item.recognized = result.get("recognized")
    item.matches = result.get("matches")
    _apply_identity_outcome(item, result)
    item.error = None
    item.lease_token = None
    item.lease_expires_at = None
    item.next_attempt_at = None
    item.retry_reason = None
    item.updated_at = now
    _refresh_job_status(db, item.job, now)
    db.commit()
    return True


def complete_claim_group(
    db: Session,
    claim: ClaimedScanItem,
    results: list[dict | None],
) -> bool:
    """Persist confident positions and queue unclear ones as individual scans."""
    now = datetime.datetime.utcnow()
    items = _leased_items(db, claim)
    if len(items) != len(claim.all_item_ids) or len(results) != len(items):
        db.rollback()
        return False
    for item, result in zip(items, results):
        if result is None:
            item.status = "pending"
            item.batch_mode = False
            item.recognized = None
            item.matches = None
            _clear_identity_outcome(item)
            item.next_attempt_at = now
            item.retry_reason = None
            # A fresh individual cycle gets a fresh catalogue allowance. The
            # outages this position met as part of a composite were charged to
            # every member of that group, and carrying them over would end the
            # individual scan on its first one.
            item.catalogue_failures = 0
        else:
            item.status = "done"
            item.recognized = result.get("recognized")
            item.matches = result.get("matches")
            _apply_identity_outcome(item, result)
            item.next_attempt_at = None
            item.retry_reason = None
        item.error = None
        item.lease_token = None
        item.lease_expires_at = None
        item.updated_at = now
    _refresh_job_status(db, items[0].job, now)
    db.commit()
    return True


def fail_claim(
    db: Session,
    claim: ClaimedScanItem,
    error: str,
    *,
    transient: bool = False,
    permanent: bool = False,
    retry_after_seconds: float | None = None,
    retry_reason: str | None = None,
) -> bool:
    now = datetime.datetime.utcnow()
    items = _leased_items(db, claim)
    if len(items) != len(claim.all_item_ids):
        db.rollback()
        return False

    for item in items:
        item.error = str(error)
        item.lease_token = None
        item.lease_expires_at = None
        if permanent:
            item.status = "failed"
            item.next_attempt_at = None
            item.retry_reason = None
        elif transient:
            item.transient_failures += 1
            if retry_reason == CATALOGUE_UNREACHABLE_RETRY_REASON:
                # Counted apart from transient_failures, which is shared with
                # unbounded reasons: two provider rate limits must not spend
                # this item's catalogue attempts before it has met one.
                item.catalogue_failures += 1
            if (
                retry_reason == CATALOGUE_UNREACHABLE_RETRY_REASON
                and item.catalogue_failures >= MAX_CATALOGUE_UNREACHABLE_ATTEMPTS
            ):
                item.status = "failed"
                item.next_attempt_at = None
                item.retry_reason = retry_reason
                # The error is what the review screen shows, and there is no
                # next attempt now, so it must not still read as though one is
                # coming.
                item.error = (
                    "The card catalogue could not be reached, so this photo was "
                    "not matched. Retrying stopped after "
                    f"{MAX_CATALOGUE_UNREACHABLE_ATTEMPTS} attempts; try it again "
                    "once the catalogue is back."
                )
                item.updated_at = now
                continue
            item.status = "retrying"
            delay = (
                max(0.0, float(retry_after_seconds))
                if retry_after_seconds is not None
                else _backoff(TRANSIENT_BACKOFF_SECONDS, item.transient_failures)
            )
            item.next_attempt_at = now + datetime.timedelta(
                seconds=delay
            )
            item.retry_reason = retry_reason
        else:
            item.attempts += 1
            item.retry_reason = None
            if item.attempts >= MAX_RECOGNITION_ATTEMPTS:
                item.status = "failed"
                item.next_attempt_at = None
            else:
                item.status = "retrying"
                item.next_attempt_at = now + datetime.timedelta(
                    seconds=_backoff(RECOGNITION_BACKOFF_SECONDS, item.attempts)
                )
        item.updated_at = now
    _refresh_job_status(db, items[0].job, now)
    db.commit()
    return True


async def default_scan_processor(
    db: Session,
    user_id: int,
    image_bytes: bytes,
    content_type: str,
    *,
    job_id: int | None = None,
    item_id: int | None = None,
) -> dict:
    """Reuse the proven single-card scanner path with background priority."""
    from api.recognize import recognize_sanitized_card
    from services.scan_providers import get_provider
    from services.scan_trace import create_scan_trace

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise PermanentScanError("The scan owner is no longer an active user.")
    provider = get_provider(db, user_id)
    trace = create_scan_trace(
        db,
        user_id,
        mode="single",
        job_id=job_id,
        item_id=item_id,
        filename="sanitized-scan.jpg",
        model=provider.model(),
    )
    trace.set_image(image_bytes)
    try:
        # Only Gemini has a shared per-key budget to protect; other providers get
        # a no-op scope rather than queueing behind Gemini's limiter.
        with provider.rate_limit_scope("background"):
            return await recognize_sanitized_card(
                db,
                user_id,
                image_bytes,
                content_type,
                trace=trace,
            )
    except Exception as exc:
        trace.record_error(str(getattr(exc, "detail", exc)))
        raise
    finally:
        trace.save()


async def default_composite_processor(
    db: Session,
    user_id: int,
    images: list[bytes],
    content_types: list[str],
    *,
    job_id: int | None = None,
    item_ids: list[int] | tuple[int, ...] | None = None,
) -> list[dict | None]:
    """Recognize a small grid and flag unclear positions for individual work."""
    from api.recognize import (
        TCGDEX_REQUEST_BURST,
        CompositeRecognitionError,
        match_composite_card_info,
        recognize_composite_card_info,
    )
    from services.scan_providers import get_provider, high_resolution_samples_enabled
    from services.card_composite import build_composite
    from services.scan_trace import create_scan_trace

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise PermanentScanError("The scan owner is no longer an active user.")
    provider = get_provider(db, user_id)
    api_key = provider.credential(db, user_id)
    # A local endpoint needs no credential, so ask the provider rather than
    # assuming an empty key means "not configured".
    if provider.requires_credential() and not api_key:
        raise PermanentScanError(
            f"No {provider.name} API key is configured for the scan owner."
        )

    trace_item_ids = list(item_ids or [])
    traces = [
        create_scan_trace(
            db,
            user_id,
            mode="composite",
            job_id=job_id,
            item_id=(trace_item_ids[position] if position < len(trace_item_ids) else None),
            filename=f"sanitized-scan-{position + 1}.jpg",
            model=provider.model(),
        )
        for position in range(len(images))
    ]
    for trace, image in zip(traces, images):
        trace.set_image(image)

    try:
        with provider.rate_limit_scope("background"):
            try:
                recognized_by_position = await recognize_composite_card_info(
                    api_key,
                    build_composite(images, high_resolution=high_resolution_samples_enabled(db)),
                    len(images),
                    traces=traces,
                    provider=provider,
                )
            except CompositeRecognitionError as exc:
                for trace in traces:
                    trace.record_error(str(exc))
                recognized_by_position = {}

            # The one paid extraction above already covered every position. What
            # is left per position is catalogue matching, which is independent
            # work dominated by TCGdex round trips, so positions overlap.
            results: list[dict | None] = [None] * len(images)
            pending: list[tuple[int, dict]] = []
            for position in range(len(images)):
                card_info = recognized_by_position.get(position)
                has_name = bool(str((card_info or {}).get("name") or "").strip())
                if not has_name:
                    traces[position].record_decision("individual_fallback")
                    continue
                pending.append((position, card_info))

            if pending:
                # Both created per call, never at module scope: an asyncio
                # primitive binds to the loop that first awaits it, and this
                # module is imported once but run under many loops.
                slots = asyncio.Semaphore(COMPOSITE_MATCH_CONCURRENCY)
                # Shared by every matcher in this claim, so the group aims no
                # more concurrent requests at TCGdex than a single matcher
                # already does today. That holds because this path passes
                # allow_visual_verification=False, which keeps each matcher
                # inside the bound TCGDEX_REQUEST_BURST is sized for; a caller
                # that allowed visual verification would need a wider gate to
                # get the same throughput, not this one.
                request_gate = asyncio.Semaphore(TCGDEX_REQUEST_BURST)

                async def match_position(position: int, card_info: dict) -> dict:
                    async with slots:
                        # Deliberately the coordinator's own session. Every
                        # query this reaches is issued and fully consumed
                        # between two awaits, so the single-threaded event loop
                        # serialises them for free. Giving each matcher its own
                        # Session instead would check out a second and third
                        # pooled connection, and each of those checkouts is a
                        # synchronous blocking wait made from inside the event
                        # loop while this coordinator already holds one - which
                        # under pool pressure stalls the whole application and
                        # fails a claim that sequential code would have
                        # completed.
                        return await match_composite_card_info(
                            db,
                            card_info,
                            photo_bytes=images[position],
                            trace=traces[position],
                            request_gate=request_gate,
                        )

                settled = await asyncio.gather(
                    *(
                        match_position(position, card_info)
                        for position, card_info in pending
                    ),
                    # Collect every outcome rather than propagating whichever
                    # failure happens to arrive first. process_claimed_scan_item
                    # maps the exception class to permanent / transient /
                    # recognition, so letting completion order pick the
                    # exception would make an item's terminal status and retry
                    # counters depend on network timing. Raising the lowest
                    # position's exception below reproduces exactly what the
                    # sequential loop did. Waiting for all of them also
                    # guarantees no matcher is still touching db when the
                    # coordinator moves on to fail the claim.
                    return_exceptions=True,
                )
                for (position, _card_info), outcome in zip(pending, settled):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    if bool(outcome.get("_identity_confident")):
                        results[position] = outcome
                    else:
                        traces[position].record_decision("individual_fallback")
            return results
    except Exception as exc:
        for trace in traces:
            trace.record_error(str(getattr(exc, "detail", exc)))
        raise
    finally:
        for trace in traces:
            trace.save()


def _scan_error_from_http(error: HTTPException) -> RuntimeError:
    if error.status_code in {429, 502, 503, 504}:
        return TransientScanError(
            str(error.detail),
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
            retry_reason=getattr(error, "retry_reason", None),
        )
    if error.status_code in {400, 401, 403}:
        return PermanentScanError(str(error.detail))
    return RecognitionScanError(str(error.detail))


async def process_claimed_scan_item(
    claim: ClaimedScanItem,
    *,
    processor=default_scan_processor,
    composite_processor=default_composite_processor,
) -> None:
    from database import SessionLocal

    db = SessionLocal()
    try:
        try:
            items = _leased_items(db, claim)
            if len(items) != len(claim.all_item_ids):
                db.rollback()
                return
            image_bytes = [resolve_scan_path(item.image_path).read_bytes() for item in items]
            user_id = items[0].user_id
            content_types = [item.content_type for item in items]
            job_id = items[0].job_id
            item_ids = [item.id for item in items]
            db.rollback()  # Release the row lock during upstream network work.
            if claim.composite:
                if composite_processor is default_composite_processor:
                    results = await composite_processor(
                        db,
                        user_id,
                        image_bytes,
                        content_types,
                        job_id=job_id,
                        item_ids=item_ids,
                    )
                else:
                    results = await composite_processor(
                        db, user_id, image_bytes, content_types
                    )
            else:
                if processor is default_scan_processor:
                    result = await processor(
                        db,
                        user_id,
                        image_bytes[0],
                        content_types[0],
                        job_id=job_id,
                        item_id=item_ids[0],
                    )
                else:
                    result = await processor(
                        db, user_id, image_bytes[0], content_types[0]
                    )
                results = [result]
        except HTTPException as exc:
            db.rollback()
            error = _scan_error_from_http(exc)
        except (FileNotFoundError, OSError, ScanUploadError) as exc:
            db.rollback()
            error = PermanentScanError(f"Stored scan photo is unavailable: {exc}")
        except (TransientScanError, RecognitionScanError, PermanentScanError) as exc:
            db.rollback()
            error = exc
        except Exception as exc:
            db.rollback()
            logger.exception("Unexpected scan processing error for item %s", claim.item_id)
            error = TransientScanError(str(exc))
        else:
            if claim.composite:
                complete_claim_group(db, claim, results)
            else:
                complete_claim(db, claim, results[0])
            return

        fail_claim(
            db,
            claim,
            str(error),
            transient=isinstance(error, TransientScanError),
            permanent=isinstance(error, PermanentScanError),
            retry_after_seconds=getattr(error, "retry_after_seconds", None),
            retry_reason=getattr(error, "retry_reason", None),
        )
    finally:
        db.close()


async def drain_scan_queue(
    *,
    max_items: int = 50,
    processor=default_scan_processor,
    composite_processor=default_composite_processor,
) -> int:
    """Process a bounded fair pass; concurrent workers safely skip claimed rows."""
    from database import SessionLocal

    processed = 0
    for _ in range(max_items):
        db = SessionLocal()
        try:
            recover_expired_leases(db)
            claim = claim_next_scan_item(db)
        finally:
            db.close()
        if claim is None:
            break
        await process_claimed_scan_item(
            claim,
            processor=processor,
            composite_processor=composite_processor,
        )
        processed += len(claim.all_item_ids)
        await asyncio.sleep(0)
    return processed


def resolve_scan_item(
    db: Session, item: ScanJobItem, *, keep_image: bool = False
) -> ScanJobItem:
    """Mark one review complete, removing its stored photo unless asked not to.

    Photos are deleted on resolution so that reviewing a batch does not leave
    the disk full of images nobody will look at again.

    A manually identified card is the exception. There the photo is the only
    record of what the recogniser could not read, and it is the evidence for
    whether a later change to retrieval would have found the card. Tracing
    keeps its own copy, but only when SCAN_TRACE_DIR is configured, so it
    cannot be relied on to retain it.
    """
    relative_path = item.image_path
    item.resolved = True
    if not keep_image:
        item.image_path = None
    item.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(item)
    if not keep_image:
        delete_scan_image(relative_path)
    return item


def retry_scan_item(db: Session, item: ScanJobItem) -> ScanJobItem:
    """Start a fresh individual recognition cycle for a reviewable item."""
    if item.resolved:
        raise ValueError("This scan has already been handled.")
    if item.status not in {"done", "failed"}:
        raise ValueError("This scan is still being processed.")
    if not item.image_path or not resolve_scan_path(item.image_path).is_file():
        raise ValueError("The stored scan photo is no longer available.")

    now = datetime.datetime.utcnow()
    _reset_item_for_rescan(item, now)
    db.commit()
    db.refresh(item)
    return item


def _reset_item_for_rescan(item: ScanJobItem, now: datetime.datetime) -> None:
    """Clear one completed item's result before it is recognised again."""
    item.status = "pending"
    item.attempts = 0
    item.transient_failures = 0
    item.catalogue_failures = 0
    item.next_attempt_at = now
    item.retry_reason = None
    item.lease_token = None
    item.lease_expires_at = None
    item.recognized = None
    item.matches = None
    _clear_identity_outcome(item)
    item.error = None
    item.batch_mode = False
    item.updated_at = now
    item.job.status = "pending"
    item.job.finished_at = None
    item.job.error_message = None
    item.job.updated_at = now


def replace_scan_item_photo(db: Session, item: ScanJobItem, raw_image: bytes) -> ScanJobItem:
    """Replace a reviewable item's photo and queue a fresh individual scan.

    Raw bytes keep validation and metadata stripping in the storage service. The
    old file is deleted only after the commit, so a failed commit leaves its row
    and file together.

    The caller's guard is not enough on its own. Sanitising and storing the
    upload takes long enough for add-all or a dismissal to lock the same row,
    file its old candidate and resolve it, so the preconditions are taken again
    here under a row lock before anything is written.
    """
    from services.scan_providers import high_resolution_samples_enabled

    sanitized = sanitize_image_bytes(
        raw_image,
        high_resolution=high_resolution_samples_enabled(db),
    )
    new_path, byte_size = store_sanitized_image(item.job.id, sanitized)
    now = datetime.datetime.utcnow()

    try:
        locked = (
            db.query(ScanJobItem)
            .filter(ScanJobItem.id == item.id)
            .with_for_update()
            .one_or_none()
        )
        if locked is None:
            raise ScanItemNoLongerReviewable("This scan no longer exists.")
        if locked.resolved:
            raise ScanItemNoLongerReviewable("This scan has already been handled.")
        if locked.status not in {"done", "failed"}:
            raise ScanItemNoLongerReviewable("This scan is still being processed.")

        old_path = locked.image_path
        locked.image_path = new_path
        locked.content_type = sanitized.content_type
        locked.byte_size = byte_size
        _reset_item_for_rescan(locked, now)
        db.commit()
    except Exception:
        db.rollback()
        delete_scan_image(new_path)
        raise

    db.refresh(item)
    # After the commit, so a re-take that really succeeded is not reported as a
    # failure because the old file could not be unlinked. The row no longer
    # references it either way, and the worst case is a leftover file the
    # retention sweep collects.
    try:
        delete_scan_image(old_path)
    except OSError:
        logger.warning("Could not remove the replaced scan photo %s", old_path)
    return item


def purge_expired_scan_jobs(db: Session, *, now: datetime.datetime | None = None) -> int:
    """Delete every job and review photo at its fixed 14-day expiry."""
    now = now or datetime.datetime.utcnow()
    jobs = db.query(ScanJob).filter(ScanJob.expires_at <= now).all()
    job_ids = [job.id for job in jobs]
    for job in jobs:
        db.delete(job)
    db.commit()
    for job_id in job_ids:
        delete_job_directory(job_id)
    return len(job_ids)


def job_progress(db: Session, job: ScanJob) -> dict:
    items = db.query(ScanJobItem).filter(ScanJobItem.job_id == job.id).all()
    counts = {status: sum(1 for item in items if item.status == status) for status in {
        "pending", "processing", "retrying", "done", "failed"
    }}
    active = counts["pending"] + counts["processing"] + counts["retrying"]
    failed_attention = sum(
        1
        for item in items
        if not item.resolved and item.status == "failed"
    )
    attention = sum(
        1
        for item in items
        if not item.resolved and item.status in {"done", "failed"}
    )
    next_retry_item = min(
        (
            item
            for item in items
            if item.status == "retrying" and item.next_attempt_at is not None
        ),
        key=lambda item: item.next_attempt_at,
        default=None,
    )
    return {
        "id": job.id,
        "status": job.status,
        "total": len(items),
        **counts,
        "processed": counts["done"] + counts["failed"],
        "active": active,
        "attention": attention,
        # The client must not guess from a rank or a null-era scan row. This is
        # the same full eligibility predicate the atomic bulk action uses.
        "confident_addable": confident_addable_count(items),
        "failed_attention": failed_attention,
        "next_retry_at": (
            next_retry_item.next_attempt_at.isoformat() if next_retry_item else None
        ),
        "retry_reason": next_retry_item.retry_reason if next_retry_item else None,
        # Kept as a stable alias for badge clients built against Lamiskin's
        # original queue payload.
        "unresolved": attention,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "expires_at": job.expires_at.isoformat() if job.expires_at else None,
        "error_message": job.error_message,
    }
