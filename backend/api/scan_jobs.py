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
from models import CollectionItem, ProductPurchase, ScanJob, ScanJobItem, User
from services.collection_options import ALLOWED_CONDITIONS
from services.tcgdex_languages import is_supported_tcgdex_language, normalize_tcgdex_language
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
    MAX_FILE_BYTES,
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


def _item_payload(
    db: Session, item: ScanJobItem, *, owned_card_ids: set[str] | None = None,
) -> dict:
    suggested_already_owned = False
    if owned_card_ids is not None:
        suggested_already_owned = str(item.suggested_match_id or "") in owned_card_ids
    elif item.identity_confident is True and item.suggested_match_id:
        suggested_already_owned = db.query(CollectionItem.id).filter(
            CollectionItem.user_id == item.user_id,
            CollectionItem.card_id == item.suggested_match_id,
            CollectionItem.quantity > 0,
        ).first() is not None
    has_image = False
    if item.image_path:
        try:
            has_image = resolve_scan_path(item.image_path).is_file()
        except ScanUploadError:
            # Do not advertise a retry just because a stale database path is
            # present. This read must not mutate review state; maintenance can
            # clean stale references separately.
            has_image = False
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
        # A suggestion can be a legitimate second copy, so this is deliberately
        # a review hint rather than permission to resolve it automatically.
        "suggested_already_owned": suggested_already_owned,
        "duplicate_scan_detected": item.duplicate_of_item_id is not None,
        "error": item.error,
        "has_image": has_image,
        # Changes when, and only when, the stored file changes. The review
        # panel fetches each photo into a blob URL once and keyed that fetch on
        # the item id, so a re-take left it showing the photo it had just
        # replaced while the scan ran against the new one. Hashed rather than
        # sent raw so the payload never carries the storage layout.
        "image_token": (
            hashlib.sha256(item.image_path.encode()).hexdigest()[:16]
            if has_image else None
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
    product_id: int | None = Form(None),
    default_condition: str | None = Form(None),
    default_lang: str | None = Form(None),
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
    session_product_id = None
    session_condition = "Mint"
    session_lang = "en"
    if product_id is not None:
        session_condition = str(default_condition or "").strip()
        session_lang = normalize_tcgdex_language(default_lang)
        if session_condition not in ALLOWED_CONDITIONS:
            raise HTTPException(status_code=422, detail="condition is not supported")
        if not is_supported_tcgdex_language(session_lang):
            raise HTTPException(status_code=422, detail="language is not supported")
        product = db.query(ProductPurchase).filter(
            ProductPurchase.id == product_id,
            ProductPurchase.user_id == current_user.id,
        ).with_for_update().first()
        if product is None:
            raise HTTPException(status_code=404, detail="Product not found.")
        from services.product_ledger import product_has_completed_sale
        if product_has_completed_sale(product):
            raise HTTPException(status_code=409, detail="A sold product cannot be scanned")
        if product.lifecycle_status != "opened":
            raise HTTPException(status_code=409, detail="Open the product before creating its scan job")
        session_product_id = product.id
    try:
        job = await create_scan_job(
            db,
            current_user.id,
            files,
            batch_modes=batch_modes,
            product_id=session_product_id,
            default_condition=session_condition,
            default_lang=session_lang,
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
    suggested_ids = {
        str(item.suggested_match_id)
        for item in items
        if item.identity_confident is True and item.suggested_match_id
    }
    owned_card_ids = {
        card_id for (card_id,) in db.query(CollectionItem.card_id).filter(
            CollectionItem.user_id == current_user.id,
            CollectionItem.quantity > 0,
            CollectionItem.card_id.in_(suggested_ids),
        ).all()
    } if suggested_ids else set()
    return {
        **job_progress(db, job),
        "items": [_item_payload(db, item, owned_card_ids=owned_card_ids) for item in items],
    }


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
    manual_correction = False
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

        manual_correction = tcg_card_id not in allowed_ids
        record_ground_truth(
            current_user.id,
            job_id,
            item_id,
            tcg_card_id,
            source="manual" if manual_correction else "candidate",
        )
        # Three outcomes, not two, and only one of them is worth no photo.
        #
        #   never offered      -> retrieval failed; the photo is the only
        #                         record of what could not be read
        #   offered, not first -> ranking missed; the photo is the evidence
        #                         for why the right card ranked below a wrong
        #                         one
        #   the top suggestion -> nothing was corrected; the candidate list
        #                         and trace already describe it
        #
        # Keeping only the first threw away half the correction evidence.
        # Keeping all three would retain a photo for every scan ever
        # confirmed, which on a household instance is the bulk of them.
        suggested_id, _ = pokemon_api.strip_lang_suffix(
            str(item.suggested_match_id or "")
        )
        corrected_the_suggestion = tcg_card_id != suggested_id
        keep_image = manual_correction or corrected_the_suggestion
    else:
        keep_image = False
    try:
        resolved = resolve_scan_item(db, item, keep_image=keep_image)
    except ValueError as exc:
        # The pre-flight check is only advisory: an add-all, re-take, or second
        # dismiss can win the row lock while catalogue preparation runs.
        raise HTTPException(status_code=409, detail=str(exc))
    return _item_payload(db, resolved)


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
    return _item_payload(db, item)


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
    # Bound the READ by the per-photo ceiling only. Charging a re-take's raw
    # upload against the remaining STORED budget refused a 15 MB photograph
    # that sanitises to 2 MB and would have fitted, and photographs usually do
    # get smaller. replace_scan_item_photo enforces the job limit on what
    # actually lands on disk, which is the only place the real cost is known.
    remaining_bytes = MAX_FILE_BYTES
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
    except ScanJobBytesExceeded as exc:
        # Must precede ScanUploadError, which it subclasses. The job being too
        # large is a 409 wherever it is detected, and only the post-store check
        # can detect it when re-encoding is what pushed it over.
        raise HTTPException(status_code=409, detail=str(exc))
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    background_tasks.add_task(drain_scan_queue, max_items=1)
    return _item_payload(db, item)


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
