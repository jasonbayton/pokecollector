"""Authenticated API for persistent background card-scan jobs."""

from __future__ import annotations

import hashlib
import json
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.auth import get_current_user
from database import get_db
from models import ScanJob, ScanJobItem, User
from services.scan_queue import (
    drain_scan_queue,
    job_progress,
    replace_scan_item_photo,
    resolve_scan_item,
    retry_scan_item,
)
from services.scan_bulk_add import (
    add_all_confident_scan_items,
    candidate_ids_to_prepare,
)
from services.scan_storage import (
    MAX_JOB_BYTES,
    ScanItemNoLongerReviewable,
    ScanJobBytesExceeded,
    ScanUploadError,
    create_scan_job,
    delete_job_directory,
    read_limited_upload,
    resolve_scan_path,
)

router = APIRouter()


class ResolveScanItemRequest(BaseModel):
    card_id: str | None = None
    lang: str | None = None


def _get_own_job(db: Session, job_id: int, current_user: User) -> ScanJob:
    job = (
        db.query(ScanJob)
        .filter(ScanJob.id == job_id, ScanJob.user_id == current_user.id)
        .first()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Scan job not found.")
    return job


def _get_own_item(
    db: Session,
    job_id: int,
    item_id: int,
    current_user: User,
) -> ScanJobItem:
    _get_own_job(db, job_id, current_user)
    item = (
        db.query(ScanJobItem)
        .filter(ScanJobItem.id == item_id, ScanJobItem.job_id == job_id)
        .first()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Scan item not found.")
    return item


def _item_payload(item: ScanJobItem) -> dict:
    return {
        "id": item.id,
        "position": item.position,
        "batch_mode": item.batch_mode,
        "status": item.status,
        "resolved": item.resolved,
        "attempts": item.attempts,
        "transient_failures": item.transient_failures,
        "recognized": item.recognized,
        "matches": item.matches,
        # Null means no verdict was ever recorded for this scan, which is not
        # the same as the matcher having been unsure. The review UI needs the
        # difference to stay honest about what it does and does not know.
        "identity_confident": item.identity_confident,
        "identity_decision": item.identity_decision,
        "suggested_match_id": item.suggested_match_id,
        "error": item.error,
        "has_image": bool(item.image_path),
        # Changes when, and only when, the stored file changes. The review
        # panel fetches each photo into a blob URL once and keyed that fetch on
        # the item id, so a re-take left it showing the photo it had just
        # replaced while the scan ran against the new one. Hashed rather than
        # sent raw so the payload never carries the storage layout.
        "image_token": (
            hashlib.sha256(item.image_path.encode()).hexdigest()[:16]
            if item.image_path else None
        ),
        "next_attempt_at": (
            item.next_attempt_at.isoformat() if item.next_attempt_at else None
        ),
        "retry_reason": item.retry_reason,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


@router.post("/recognize/jobs")
async def enqueue_scan_job(
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(...),
    individual_positions: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Sanitize a batch, persist it, and return without waiting for Gemini."""
    from services.scan_providers import get_provider

    provider = get_provider(db, current_user.id)
    if provider.requires_credential() and not provider.credential(db, current_user.id):
        raise HTTPException(
            status_code=400,
            detail=provider.missing_credential_message(),
        )
    try:
        requested_individual = json.loads(individual_positions or "[]")
        if (
            not isinstance(requested_individual, list)
            or any(type(position) is not int for position in requested_individual)
            or len(set(requested_individual)) != len(requested_individual)
            or any(position < 0 or position >= len(files) for position in requested_individual)
        ):
            raise ValueError
    except (TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid individual scan selection.")

    individual_set = set(requested_individual)
    batch_modes = [
        len(files) > 1 and position not in individual_set
        for position in range(len(files))
    ]
    try:
        job = await create_scan_job(
            db,
            current_user.id,
            files,
            batch_modes=batch_modes,
        )
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    background_tasks.add_task(drain_scan_queue, max_items=len(files))
    return job_progress(db, job)


@router.get("/recognize/jobs")
def list_scan_jobs(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return active or actionable jobs for the current user's scan inbox."""
    jobs = (
        db.query(ScanJob)
        .join(ScanJobItem, ScanJobItem.job_id == ScanJob.id)
        .filter(
            ScanJob.user_id == current_user.id,
            ScanJobItem.resolved.is_(False),
        )
        .distinct()
        .order_by(ScanJob.created_at.desc())
        .limit(50)
        .all()
    )
    return {"jobs": [job_progress(db, job) for job in jobs]}


@router.get("/recognize/jobs/{job_id}")
def get_scan_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_own_job(db, job_id, current_user)
    items = (
        db.query(ScanJobItem)
        .filter(ScanJobItem.job_id == job.id, ScanJobItem.resolved.is_(False))
        .order_by(ScanJobItem.position.asc())
        .all()
    )
    return {**job_progress(db, job), "items": [_item_payload(item) for item in items]}


@router.get("/recognize/jobs/{job_id}/items/{item_id}/image")
def get_scan_job_item_image(
    job_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_own_item(db, job_id, item_id, current_user)
    if not item.image_path:
        raise HTTPException(status_code=404, detail="Scan photo not found.")
    try:
        path = resolve_scan_path(item.image_path)
    except ScanUploadError:
        raise HTTPException(status_code=404, detail="Scan photo not found.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Scan photo not found.")
    return FileResponse(path, media_type="image/jpeg", filename="scan.jpg")


@router.post("/recognize/jobs/{job_id}/items/{item_id}/resolve")
def resolve_scan_job_item(
    job_id: int,
    item_id: int,
    data: ResolveScanItemRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_own_item(db, job_id, item_id, current_user)
    if item.status not in {"done", "failed"}:
        raise HTTPException(status_code=409, detail="This scan is still being processed.")
    card_id = str((data.card_id if data else "") or "").strip() or None
    if card_id:
        from api.collection import ensure_card_exists
        from services import pokemon_api
        allowed_ids = {
            str(match.get("tcg_card_id") or "")
            for match in (item.matches or [])
            if isinstance(match, dict)
        }
        tcg_card_id, _ = pokemon_api.strip_lang_suffix(card_id)
        selected_lang = str((data.lang if data else "") or "en").strip() or "en"
        # Do not trust the picker: confirming a card must still prove that the
        # exact catalogue printing exists in a supported language. This can
        # fetch and commit, so it deliberately runs before scan resolution.
        ensure_card_exists(
            db,
            f"{tcg_card_id}_{selected_lang}",
            lang=selected_lang,
            user_id=current_user.id,
        )
        from services.scan_trace import record_ground_truth

        record_ground_truth(
            current_user.id,
            job_id,
            item_id,
            tcg_card_id,
            source="candidate" if tcg_card_id in allowed_ids else "manual",
        )
    return _item_payload(resolve_scan_item(db, item))


@router.post("/recognize/jobs/{job_id}/add-all-confident")
def add_all_confident_scan_job_items(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atomically file this job's confidence-backed scan candidates.

    ``ensure_card_exists`` may fetch from the catalogue and commit.  It must run
    before the job's PostgreSQL row lock so an upstream delay never blocks scan
    review or a duplicate request.  The locked write path validates candidates
    again, so this preparation read cannot authorize a stale suggestion.
    """
    job = _get_own_job(db, job_id, current_user)
    items = (
        db.query(ScanJobItem)
        .filter(ScanJobItem.job_id == job.id)
        .order_by(ScanJobItem.position.asc())
        .all()
    )

    from api.collection import ensure_card_exists

    prepared_card_ids: set[str] = set()
    for card_id in candidate_ids_to_prepare(items):
        card = ensure_card_exists(db, card_id, user_id=current_user.id)
        prepared_card_ids.add(card.id)

    # Release any read transaction before the locking write path begins.
    db.rollback()
    added = add_all_confident_scan_items(
        db,
        job_id=job_id,
        current_user=current_user,
        prepared_card_ids=prepared_card_ids,
    )
    return {"added": added, "condition": "Mint"}


@router.post("/recognize/jobs/{job_id}/items/{item_id}/retry")
async def retry_scan_job_item(
    job_id: int,
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    item = _get_own_item(db, job_id, item_id, current_user)
    try:
        retry_scan_item(db, item)
    except (ValueError, ScanUploadError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    background_tasks.add_task(drain_scan_queue, max_items=1)
    return _item_payload(item)


@router.post("/recognize/jobs/{job_id}/items/{item_id}/photo")
async def replace_scan_job_item_photo(
    job_id: int,
    item_id: int,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(..., alias="file"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Replace one completed scan photo and schedule it for a fresh scan."""
    item = _get_own_item(db, job_id, item_id, current_user)
    if item.resolved:
        raise HTTPException(status_code=409, detail="This scan has already been handled.")
    if item.status not in {"done", "failed"}:
        raise HTTPException(status_code=409, detail="This scan is still being processed.")
    if len(files) != 1:
        raise HTTPException(status_code=400, detail="Exactly one scan photo is required.")

    current_bytes = sum(
        int(candidate.byte_size or 0)
        for candidate in item.job.items
        if candidate.image_path
    )
    remaining_bytes = MAX_JOB_BYTES - (current_bytes - int(item.byte_size or 0))
    try:
        raw_image = await read_limited_upload(files[0], remaining_job_bytes=remaining_bytes)
    except ScanJobBytesExceeded as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        replace_scan_item_photo(db, item, raw_image)
    except ScanItemNoLongerReviewable as exc:
        # Another review action claimed the row while the upload was being
        # sanitised. Same status as the guard above, because from the user's
        # side it is the same refusal.
        raise HTTPException(status_code=409, detail=str(exc))
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    background_tasks.add_task(drain_scan_queue, max_items=1)
    return _item_payload(item)


@router.delete("/recognize/jobs/{job_id}")
def delete_scan_job(
    job_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = _get_own_job(db, job_id, current_user)
    db.delete(job)
    db.commit()
    delete_job_directory(job_id)
    return {"deleted": job_id}
