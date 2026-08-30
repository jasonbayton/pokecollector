import base64
import asyncio
import contextlib
import datetime
import httpx
import io
import math
import os
import json
import re
import unicodedata
import warnings
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urlparse
from services.tcgdex_languages import is_supported_tcgdex_language, normalize_tcgdex_language
from services.gemini_rate_limit import (
    GeminiKeyBlockedError,
    acquire_gemini_slot,
    penalize_gemini_key,
    record_gemini_success,
)
from services.scan_storage import MAX_FILE_BYTES, ScanUploadError, read_limited_upload, sanitize_image_bytes
from services.scan_trace import ScanTrace, create_scan_trace
from services.scan_providers import (
    GEMINI,
    ScanProvider,
    get_provider,
    image_part,
    image_part_from_bytes,
    text_part,
    visual_verification_enabled,
)
import logging
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy import and_, func, or_
from services.card_numbers import card_number_variants
from services.tcgdex_assets import secure_asset_url, trusted_asset_hosts
from services.pokemon_api import (
    get_base_url as tcgdex_base_url,
    get_standby_base_url as tcgdex_standby_base_url,
)
from services.scan_queue import CATALOGUE_UNREACHABLE_RETRY_REASON
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database import get_db
from models import Card, Setting, UserSetting, User, Set

logger = logging.getLogger(__name__)

router = APIRouter()

GEMINI_TRANSIENT_STATUS_CODES = {502, 503, 504}
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
GEMINI_MODELS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_GEMINI_RETRY_SECONDS = 14 * 24 * 60 * 60
PHASH_MAX_DISTANCE = 20
PHASH_MIN_MARGIN = 5
PHASH_CANDIDATE_LIMIT = 8
CANDIDATE_DETAIL_LIMIT = 8
MAX_REFERENCE_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_IMAGE_PIXELS = 50_000_000
# The catalogue CDN, plus an HTTPS asset mirror when one is configured. An
# HTTP mirror is deliberately excluded: these bytes are decoded as an image and
# sent to the vision model, and a mirror on a plain LAN address must not be
# able to relax that. Such a mirror still serves the pictures people look at,
# which go through the app's own image endpoint.
TRUSTED_REFERENCE_IMAGE_HOSTS = trusted_asset_hosts()
# Which catalogue a candidate came from, carried on the candidate so its later
# detail lookup goes back to the same one.
CATALOGUE_PRIMARY = "primary"
CATALOGUE_STANDBY = "standby"
# The widest burst one gated matcher can aim at TCGdex.
#
# A matcher fans out concurrently in exactly two places, and they run in
# different phases, so its peak is the larger of the two rather than their sum:
# _fill_candidate_details gathers over at most CANDIDATE_DETAIL_LIMIT candidates,
# and the pHash download gathers over at most PHASH_CANDIDATE_LIMIT. The searches
# in _search_and_rank_candidates are a sequential loop and add nothing.
#
# One fan-out is wider: _download_candidate_images over every retained candidate
# (up to 8 baseline plus 4 late number matches). It is reached only from the
# visual verification branch, and every caller that shares a gate today is the
# composite path, which hard-sets allow_visual_verification=False. Sizing the
# gate for a burst that path cannot produce would let a group of matchers exceed
# the peak of any one of them, which is the opposite of the point.
#
# Sharing a gate of this size across several matchers therefore holds the group
# at one matcher's fan-out instead of multiplying it.
#
# What the tests actually establish: that the gate is used, and that removing it
# lets the in-flight count double. They cannot establish the peak a real TCGdex
# client reaches, because they drive a fake one. Two earlier tests claimed to
# pin that peak and were deleted: they asserted on numbers their own fixture
# produced, so they would have passed whatever the real path did.
TCGDEX_REQUEST_BURST = max(PHASH_CANDIDATE_LIMIT, CANDIDATE_DETAIL_LIMIT)


def _request_gate(request_gate):
    """Use the caller's gate, or nothing at all when it did not supply one.

    Deliberately opt-in. The individual scan path passes nothing and keeps
    exactly today's fan-out, so nothing the web app serves ends up queueing
    behind a background worker's downloads.
    """
    return request_gate if request_gate is not None else contextlib.nullcontext()


class CatalogueUnreachableHTTPException(HTTPException):
    """A 503 the scan queue can recognise and stop retrying indefinitely.

    Every retry of this failure re-runs the vision extraction first, which on a
    metered provider is paid for again, and the catalogue being down is not
    something a retry an hour later is likely to fix during a long outage.
    Tagging the reason lets the queue bound these attempts without changing how
    any other transient failure is retried.
    """

    def __init__(self, detail: str):
        self.retry_reason = CATALOGUE_UNREACHABLE_RETRY_REASON
        super().__init__(status_code=503, detail=detail)


class GeminiRateLimitHTTPException(HTTPException):
    """A 429 carrying machine-readable retry metadata for the scan queue."""

    def __init__(self, *, retry_after_seconds: float, retry_reason: str):
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.retry_reason = retry_reason
        super().__init__(
            status_code=429,
            detail="Gemini rate limit reached. Try again after the stated wait.",
            headers={"Retry-After": str(max(1, int(self.retry_after_seconds + 0.999)))},
        )


def _normalize_collector_number(value) -> str | None:
    """Normalize a complete numeric or prefixed collector number."""
    if value is None:
        return None
    local = str(value).split("/", 1)[0].strip()
    compact = re.sub(r"[\s_-]+", "", local)
    match = re.fullmatch(r"([A-Za-z]*)(\d+)([A-Za-z]*)", compact)
    if not match:
        return None
    prefix, digits, suffix = match.groups()
    return f"{prefix.casefold()}{int(digits)}{suffix.casefold()}"


def normalize_scanner_card_number(value) -> str | None:
    """Normalize a collector number while preserving identity prefixes."""
    return _normalize_collector_number(value)


def prioritize_cards_by_number(
    cards: list[dict],
    recognized_number,
    *,
    number_field: str = "number",
) -> tuple[list[dict], int]:
    """Stable-partition cards so recognized collector-number matches come first."""
    target_number = normalize_scanner_card_number(recognized_number)
    if not target_number:
        return cards, 0

    matches = []
    rest = []
    for card in cards:
        candidate_number = normalize_scanner_card_number(card.get(number_field))
        (matches if candidate_number == target_number else rest).append(card)

    if not matches:
        return cards, 0
    return matches + rest, len(matches)


def select_search_candidates(
    cards: list[dict],
    recognized_number,
    *,
    number_field: str = "number",
    baseline_limit: int = 8,
    matching_extra_limit: int = 4,
) -> list[dict]:
    """Keep baseline search results and append bounded number matches."""
    selected = list(cards[:baseline_limit])
    for card in selected:
        card["_number_extra"] = False
    target = normalize_scanner_card_number(recognized_number)
    if not target:
        return selected
    extras = 0
    selected_ids = {id(card) for card in selected}
    for card in cards[baseline_limit:]:
        if extras >= matching_extra_limit:
            break
        if normalize_scanner_card_number(card.get(number_field)) != target:
            continue
        if id(card) not in selected_ids:
            card["_number_extra"] = True
            selected.append(card)
            selected_ids.add(id(card))
            extras += 1
    return selected


def retain_ranked_candidates(
    candidates: list[dict],
    *,
    baseline_limit: int = 8,
    matching_extra_limit: int = 4,
) -> list[dict]:
    """Retain ranked baseline results plus bounded late number matches."""
    baseline = [card for card in candidates if not card.get("_number_extra")][
        :baseline_limit
    ]
    extras = [card for card in candidates if card.get("_number_extra")][
        :matching_extra_limit
    ]
    retained_ids = {id(card) for card in baseline + extras}
    return [card for card in candidates if id(card) in retained_ids]


def split_recognized_card_number(value) -> tuple[str | None, str | None]:
    """Split a legacy combined collector number without losing new split fields."""
    if value is None:
        return None, None
    parts = [part.strip() for part in str(value).split("/", 1)]
    local = parts[0] or None
    total = (parts[1] or None) if len(parts) > 1 else None
    return local, total


def normalize_recognized_card_info(card_info: dict | None) -> dict:
    """Keep one canonical split identity while preserving the UI's combined number."""
    normalized = dict(card_info or {})
    legacy_local, legacy_total = split_recognized_card_number(normalized.get("number"))
    local = normalized.get("number_local") or legacy_local
    total = normalized.get("number_total") or legacy_total
    normalized["number_local"] = local
    normalized["number_total"] = total
    normalized["number"] = (
        f"{local}/{total}" if local and total else (str(local) if local else None)
    )
    return normalized


def _normalize_number(value) -> str | None:
    return _normalize_collector_number(value)


def _numbers_match(left, right) -> bool:
    normalized_left = _normalize_number(left)
    normalized_right = _normalize_number(right)
    return normalized_left is not None and normalized_left == normalized_right


def _printed_total_signal(recognized_total, candidate_total) -> int:
    """Return 0 for agreement, 1 for unknown, and 2 for contradiction."""
    normalized = _normalize_number(recognized_total)
    candidate_normalized = _normalize_number(candidate_total)
    if normalized is None or candidate_normalized is None:
        return 1
    return 0 if normalized == candidate_normalized else 2


_ARTIST_PREFIX = re.compile(
    r"^\s*(?:illus|illustrator|art|artwork)(?:\s*[.:]\s*|\s+by\s+|\s+)",
    re.IGNORECASE,
)


def _normalize_artist(value) -> str | None:
    if not value:
        return None
    stripped = _ARTIST_PREFIX.sub("", str(value))
    collapsed = " ".join(stripped.split()).strip().casefold()
    return collapsed or None


def _artists_match(left, right) -> bool:
    normalized_left = _normalize_artist(left)
    normalized_right = _normalize_artist(right)
    return normalized_left is not None and normalized_left == normalized_right


def get_gemini_model() -> str:
    """Return the configured Gemini model name without the optional models/ prefix."""
    model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip()
    if not model:
        model = DEFAULT_GEMINI_MODEL
    if model.startswith("models/"):
        model = model.removeprefix("models/")
    return model


def build_gemini_generate_url(model: str | None = None) -> str:
    """Build the Gemini generateContent endpoint for the configured scanner model."""
    gemini_model = (model or get_gemini_model()).strip()
    if gemini_model.startswith("models/"):
        gemini_model = gemini_model.removeprefix("models/")
    return f"{GEMINI_MODELS_BASE_URL}/{gemini_model}:generateContent"


def gemini_error_message(resp: httpx.Response) -> str:
    """Extract the useful upstream Gemini error body when available."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message or "").strip()


def gemini_retry_after_seconds(resp: httpx.Response) -> float | None:
    """Read Gemini's retry hint from a header or google.rpc.RetryInfo body."""
    def valid_delay(value: float) -> float | None:
        return (
            value
            if math.isfinite(value) and 0 < value <= MAX_GEMINI_RETRY_SECONDS
            else None
        )

    header = str(resp.headers.get("retry-after", "")).strip()
    if header:
        try:
            value = valid_delay(float(header))
            if value is not None:
                return value
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(header)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=datetime.timezone.utc)
                response_date = str(resp.headers.get("date", "")).strip()
                baseline = (
                    parsedate_to_datetime(response_date)
                    if response_date
                    else datetime.datetime.now(datetime.timezone.utc)
                )
                if baseline.tzinfo is None:
                    baseline = baseline.replace(tzinfo=datetime.timezone.utc)
                value = valid_delay((retry_at - baseline).total_seconds())
                if value is not None:
                    return value
            except (TypeError, ValueError, OverflowError):
                pass
    try:
        payload = resp.json()
    except ValueError:
        return None
    details = payload.get("error", {}).get("details", []) if isinstance(payload, dict) else []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        delay = str(detail.get("retryDelay", "")).strip()
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)s", delay)
        if match:
            value = valid_delay(float(match.group(1)))
            if value is not None:
                return value
    return None


def gemini_rate_limit_reason(resp: httpx.Response) -> str:
    """Classify reliable requests-per-day quota signals; default to short-term."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    details = error.get("details", []) if isinstance(error, dict) else []
    signals = []
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        detail_type = str(detail.get("@type") or "")
        if not detail_type.endswith("google.rpc.QuotaFailure"):
            continue
        violations = detail.get("violations", [])
        for violation in violations if isinstance(violations, list) else []:
            if not isinstance(violation, dict):
                continue
            signals.extend(
                str(violation.get(key) or "")
                for key in ("quotaId", "quotaMetric", "subject")
            )

    normalized = " ".join(signals).lower()
    compact = re.sub(r"[^a-z0-9]+", "", normalized)
    daily_markers = (
        "requestsperday",
        "requestperday",
        "generatedrequestsperday",
        "perdayperproject",
        "perdayperuser",
        "dailyquota",
    )
    return "daily_quota" if any(marker in compact for marker in daily_markers) else "rate_limit"


def get_gemini_key(db: Session, user_id: int = None) -> str:
    """Read Gemini API key from user settings only. No cross-user fallback."""
    if user_id is not None:
        row = db.query(UserSetting).filter(
            UserSetting.user_id == user_id, UserSetting.key == "gemini_api_key"
        ).first()
        if row and row.value:
            return row.value
    # No global/env fallback — each user must configure their own key
    return ""


async def post_gemini_generate(
    client: httpx.AsyncClient,
    gemini_url: str,
    api_key: str,
    payload: dict,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    """Call Gemini with small retries for transient capacity errors."""
    last_error = None

    for attempt in range(max_attempts):
        try:
            await acquire_gemini_slot(api_key)
            resp = await client.post(
                gemini_url,
                headers={"x-goog-api-key": api_key},
                json=payload,
            )

            if resp.status_code == 429:
                retry_reason = gemini_rate_limit_reason(resp)
                retry_after = penalize_gemini_key(
                    api_key,
                    seconds=gemini_retry_after_seconds(resp),
                    reason=retry_reason,
                )
                raise GeminiRateLimitHTTPException(
                    retry_after_seconds=retry_after,
                    retry_reason=retry_reason,
                )
            if resp.status_code in {400, 401, 403}:
                raise HTTPException(
                    status_code=400,
                    detail="That Gemini API key was rejected. Check it in settings.",
                )
            if resp.status_code == 404:
                upstream_message = gemini_error_message(resp)
                detail = "That Gemini model is not available. Set GEMINI_MODEL to a supported model."
                if upstream_message:
                    detail = f"{detail} Google reports: {upstream_message}"
                raise HTTPException(status_code=502, detail=detail)
            if resp.status_code in GEMINI_TRANSIENT_STATUS_CODES:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise HTTPException(
                    status_code=503,
                    detail="Gemini is busy or unavailable right now. Try again shortly.",
                )
            if resp.is_error:
                upstream_message = gemini_error_message(resp)
                detail = f"The Gemini request failed ({resp.status_code})."
                if upstream_message:
                    detail = f"{detail} Google reports: {upstream_message}"
                raise HTTPException(status_code=502, detail=detail)
            try:
                record_gemini_success(api_key)
            except Exception:
                logger.exception("Could not reset Gemini quota state after a successful response")
            return resp
        except GeminiKeyBlockedError as error:
            raise GeminiRateLimitHTTPException(
                retry_after_seconds=error.retry_after_seconds,
                retry_reason=error.reason,
            )
        except HTTPException:
            raise
        except httpx.RequestError as e:
            last_error = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise HTTPException(
                status_code=503,
                detail="Gemini could not be reached. Check the connection, or try again later.",
            )

    raise HTTPException(status_code=500, detail=f"The Gemini request failed: {last_error}")


# A vision model reading a card is not a quick request. The single 30 second
# budget covered connecting, uploading a base64 image of several megabytes AND
# waiting for inference, so a busy provider or a large composite exhausted it
# and surfaced as httpx.RequestError - which the provider layer reports as
# "The scanner endpoint could not be reached". The endpoint was reachable; it
# was thinking.
#
# Split apart, connect stays short so a genuinely unreachable host still fails
# fast, while read is generous because that is the part that legitimately
# takes minutes.
def recognition_timeout() -> httpx.Timeout:
    return httpx.Timeout(connect=10.0, read=180.0, write=120.0, pool=10.0)



RECOGNIZE_PROMPT = """Look at this Pokemon Trading Card Game card image.

IMPORTANT ACCURACY RULES:
- Only report text or symbols that are actually visible in this image.
- First read the printed set code and local collector number. Together these identify
  the printing. Use the name, artwork and all other details only to confirm that result.
- A complete number_local has every character visible. If one or more characters are
  unreadable but the position is clear, preserve each unreadable character as `?`, for
  example `2?5`. Use null only when the number is absent or no useful pattern is visible.
- number_total, set_code, regulation_mark, and artist are small printed details. Return
  null if any of their characters are unclear instead of guessing.
- Only return set_code when printed alphanumeric characters are visible near the card
  number. Do not infer a code from the artwork or from recognizing the set.

Extract:
1. Printed set code/abbreviation, or null
2. Local collector number, a partial pattern using `?`, or null
3. Printed set total/denominator, or null
4. Two-letter ISO language code
5. Card name exactly as printed, in the card's language, for confirmation
6. English card name, for confirmation (same value when already English)
7. Card type: Pokemon, Trainer, or Energy
8. HP value, or null
9. Regulation mark, or null
10. Artist/illustrator credit, or null

Respond ONLY with this exact JSON:
{
  "name": "...",
  "name_en": "...",
  "number_local": null,
  "number_total": null,
  "set_code": null,
  "regulation_mark": null,
  "card_type": "Pokemon/Trainer/Energy",
  "hp": null,
  "language": "en",
  "artist": null
}
Replace a null only when that exact value is clearly visible."""


_SUFFIXES = re.compile(
    r"[\s-]+(?:EX|ex|GX|gx|V|VMAX|VSTAR|VStar|TAG\s*TEAM|BREAK|LV\.?\s*X)\s*$",
    re.IGNORECASE,
)


def _simplify_name(name: str) -> str:
    return _SUFFIXES.sub("", name).strip()


def _identity_signal(target, candidate, matcher) -> int:
    """Return 0 for agreement, 1 for unknown, and 2 for contradiction."""
    if target in (None, "") or candidate in (None, ""):
        return 1
    return 0 if matcher(target, candidate) else 2


def _confirmation_name(value) -> str | None:
    """Normalise a name only for the post-retrieval confirmation check.

    Keeps alphanumerics of ANY script. Restricting this to ASCII a-z0-9
    silently reduced every Japanese, Chinese and Korean name to nothing, so
    two different cards both normalised to empty and were read as agreeing -
    which let a code-and-number lookup confidently file a card whose name
    contradicted the photograph. Compatibility-normalise first so full-width
    and half-width forms of the same name compare equal.
    """
    text = unicodedata.normalize("NFKC", _simplify_name(str(value or ""))).casefold()
    text = "".join(ch for ch in text if ch.isalnum())
    return text or None


def _code_number_name_agrees(card_info: dict, candidate: dict) -> bool:
    """A readable name may confirm a direct printing lookup, never retrieve it.

    Compares against the recognised name in the CANDIDATE's own language where
    that is known, because a Japanese candidate legitimately does not match an
    English name_en and treating that as a contradiction would reject correct
    results. Where the language is unknown, either reading may confirm.
    """
    candidate_name = _confirmation_name(candidate.get("name"))
    if candidate_name is None:
        # Nothing to confirm against: unknown, not disagreement.
        return True

    candidate_lang = str(candidate.get("lang") or candidate.get("_lang") or "").strip().lower()
    recognized_lang = str(card_info.get("language") or "").strip().lower()
    if candidate_lang and recognized_lang:
        field = "name" if candidate_lang == recognized_lang else "name_en"
        expected = _confirmation_name(card_info.get(field))
        if expected is None and field == "name_en":
            expected = _confirmation_name(card_info.get("name"))
        return expected is None or candidate_name == expected

    recognized_names = {
        _confirmation_name(card_info.get(field))
        for field in ("name", "name_en")
    }
    recognized_names.discard(None)
    return not recognized_names or candidate_name in recognized_names


def _candidate_rank_key(card_info: dict, candidate: dict) -> tuple[int, ...]:
    return (
        _identity_signal(card_info.get("number_local"), candidate.get("number"), _numbers_match),
        _identity_signal(
            normalize_tcgdex_language(card_info.get("language"))
            if card_info.get("language") else None,
            normalize_tcgdex_language(candidate.get("_lang"))
            if candidate.get("_lang") else None,
            lambda left, right: left == right,
        ),
        _printed_total_signal(
            card_info.get("number_total"), candidate.get("printed_total")
        ),
        _identity_signal(
            str(card_info.get("set_code") or "").strip().casefold() or None,
            str(candidate.get("set_abbreviation") or "").strip().casefold() or None,
            lambda left, right: left == right,
        ),
        _identity_signal(
            str(card_info.get("regulation_mark") or "").strip().casefold() or None,
            str(candidate.get("regulation_mark") or "").strip().casefold() or None,
            lambda left, right: left == right,
        ),
        _identity_signal(card_info.get("artist"), candidate.get("artist"), _artists_match),
        _identity_signal(card_info.get("hp"), candidate.get("hp"), _numbers_match),
    )


def _confirmed_identity_signals(card_info: dict, candidate: dict) -> set[str]:
    names = ("number", "language", "total", "set", "regulation", "artist", "hp")
    return {
        name
        for name, score in zip(names, _candidate_rank_key(card_info, candidate))
        if score == 0
    }


def _metadata_decision(card_info: dict, candidates: list[dict]) -> tuple[bool, str | None]:
    """Decide only when reliable metadata isolates one candidate."""
    if not candidates:
        return False, None
    ranked = sorted(candidates, key=lambda card: _candidate_rank_key(card_info, card))
    top = ranked[0]
    top_key = _candidate_rank_key(card_info, top)
    if sum(1 for card in ranked if _candidate_rank_key(card_info, card) == top_key) != 1:
        return False, None

    # Contradictory known metadata cannot identify one clear printing. An
    # individual scan may still use visual verification; a composite safely
    # falls back to recognizing only that source photo again.
    if 2 in top_key:
        return False, None

    signals = _confirmed_identity_signals(card_info, top)
    number_matches = sum(
        1
        for card in ranked
        if _identity_signal(card_info.get("number_local"), card.get("number"), _numbers_match) == 0
    )
    if "number" in signals and number_matches == 1:
        return True, "number_unique"
    if "number" in signals and signals.intersection(
        {"language", "total", "set", "regulation"}
    ):
        return True, "number_metadata"
    if not card_info.get("number_local") and {"artist", "hp"}.issubset(signals):
        return True, "artist_hp"
    return False, None


async def _download_candidate_images(
    client: httpx.AsyncClient,
    candidates: list[dict],
    existing: dict[str, bytes] | None = None,
    *,
    request_gate=None,
) -> dict[str, bytes]:
    """Download each candidate image at most once for pHash/Gemini reuse."""
    downloaded = dict(existing or {})

    async def fetch(candidate: dict) -> tuple[str, bytes] | None:
        candidate_id = str(candidate.get("id") or "")
        image_url = candidate.get("image")
        if not candidate_id or not image_url or candidate_id in downloaded:
            return None
        image_url = secure_asset_url(image_url)
        parsed_url = urlparse(str(image_url))
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname not in TRUSTED_REFERENCE_IMAGE_HOSTS
        ):
            return None
        try:
            async with _request_gate(request_gate):
                async with client.stream("GET", image_url, timeout=5) as response:
                    if response.status_code != 200:
                        return None
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            if int(content_length) > MAX_REFERENCE_IMAGE_BYTES:
                                return None
                        except ValueError:
                            return None

                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > MAX_REFERENCE_IMAGE_BYTES:
                            return None
                        content.extend(chunk)
                    if content:
                        return candidate_id, bytes(content)
        except Exception:
            return None
        return None

    results = await asyncio.gather(*(fetch(candidate) for candidate in candidates))
    downloaded.update(result for result in results if result is not None)
    return downloaded


@lru_cache(maxsize=1)
def _phash_dct_matrix():
    """Build the unnormalised DCT-II matrix used by imagehash.phash."""
    import numpy as np

    size = 32
    positions = np.arange(size)
    frequencies = np.arange(size)[:, None]
    return 2 * np.cos(
        np.pi * frequencies * (2 * positions + 1) / (2 * size)
    )


def _perceptual_hash(image_bytes: bytes | None) -> tuple[bool, ...] | None:
    """Return the same 64-bit pHash as imagehash without its SciPy dependency."""
    if not image_bytes:
        return None
    try:
        import numpy as np
        from PIL import Image

        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = image.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > MAX_REFERENCE_IMAGE_PIXELS
                ):
                    return None
                pixels = np.asarray(
                    image.convert("L").resize(
                        (32, 32),
                        Image.Resampling.LANCZOS,
                    ),
                    dtype=float,
                )
        transform = _phash_dct_matrix()
        low_frequencies = (transform @ pixels @ transform.T)[:8, :8]
        median = np.median(low_frequencies)
        return tuple(bool(value) for value in (low_frequencies > median).flat)
    except Exception:
        return None


def _phash_best_match(
    candidates: list[dict],
    photo_bytes: bytes | None,
    candidate_images: dict[str, bytes],
    trace: ScanTrace | None = None,
) -> dict | None:
    """Return a clearly separated perceptual match, otherwise abstain."""
    photo_hash = _perceptual_hash(photo_bytes)
    if photo_hash is None:
        return None

    scored: list[tuple[int, dict]] = []
    for candidate in candidates[:PHASH_CANDIDATE_LIMIT]:
        image_bytes = candidate_images.get(str(candidate.get("id") or ""))
        if not image_bytes:
            continue
        candidate_hash = _perceptual_hash(image_bytes)
        if candidate_hash is None:
            continue
        distance = sum(left != right for left, right in zip(photo_hash, candidate_hash))
        scored.append((distance, candidate))

    if len(scored) < 2:
        if trace:
            trace.record_phash(
                [
                    (distance, str(candidate.get("tcg_card_id") or ""))
                    for distance, candidate in scored
                ],
                accepted=None,
                reason="insufficient_images",
            )
        return None
    scored.sort(key=lambda pair: pair[0])
    best_distance, best_candidate = scored[0]
    runner_up_distance = scored[1][0]
    too_far = best_distance > PHASH_MAX_DISTANCE
    too_close = runner_up_distance - best_distance < PHASH_MIN_MARGIN
    accepted = None if too_far or too_close else best_candidate
    if trace:
        trace.record_phash(
            [
                (distance, str(candidate.get("tcg_card_id") or ""))
                for distance, candidate in scored
            ],
            accepted=(
                str(accepted.get("tcg_card_id") or "") if accepted else None
            ),
            reason=(
                "too_far" if too_far else "ambiguous_margin" if too_close else "accepted"
            ),
        )
    if accepted is None:
        return None
    return accepted


async def _fill_candidate_details(
    db: Session,
    candidates: list[dict],
    card_info: dict,
    *,
    limit: int = CANDIDATE_DETAIL_LIMIT,
    request_gate=None,
) -> None:
    """Fill only metadata needed by the deterministic ranker, local DB first."""
    targets = candidates[:limit]
    required_fields = {
        candidate_field
        for recognized_field, candidate_field in (
            ("artist", "artist"),
            ("hp", "hp"),
            ("regulation_mark", "regulation_mark"),
            ("number_total", "printed_total"),
        )
        if card_info.get(recognized_field) not in (None, "")
    }
    if not targets or not required_fields:
        return

    ids = [card["id"] for card in targets if card.get("id")]
    if ids and required_fields.intersection({"artist", "hp", "regulation_mark"}):
        # Issued and fully consumed without an await in between, so concurrent
        # callers sharing one Session cannot interleave here. Keep it that way:
        # awaiting between the query and the last read of `rows` would let a
        # second matcher re-enter the session mid-transaction.
        rows = db.query(Card.id, Card.artist, Card.hp, Card.regulation_mark).filter(
            Card.id.in_(ids)
        ).all()
        local = {row.id: row for row in rows}
        for card in targets:
            row = local.get(card.get("id"))
            if row:
                card["artist"] = card.get("artist") or row.artist
                card["hp"] = card.get("hp") or row.hp
                card["regulation_mark"] = card.get("regulation_mark") or row.regulation_mark

    missing = [
        card
        for card in targets
        if any(not card.get(field) for field in required_fields)
    ]
    if not missing:
        return

    async with httpx.AsyncClient(timeout=8) as client:
        async def fetch(card):
            tcg_id = card.get("tcg_card_id")
            language = card.get("_lang", "en")
            if not tcg_id:
                return
            if card.get("_from_local_catalogue"):
                # These came from the synced rows because no catalogue could be
                # reached, and the rows have already been read for these fields
                # above. Asking the network again would only wait out a timeout
                # against a host known to be down.
                return
            base_url = _catalogue_base_for(card.get("_catalogue"))
            if base_url is None:
                return
            try:
                async with _request_gate(request_gate):
                    response = await client.get(
                        f"{base_url(language)}/cards/{tcg_id}"
                    )
                if response.status_code != 200:
                    return
                detail = response.json()
                card["artist"] = card.get("artist") or detail.get("illustrator")
                card["hp"] = card.get("hp") or detail.get("hp")
                card["regulation_mark"] = (
                    card.get("regulation_mark") or detail.get("regulationMark")
                )
                official_total = ((detail.get("set") or {}).get("cardCount") or {}).get("official")
                card["printed_total"] = card.get("printed_total") or official_total
            except Exception:
                return

        await asyncio.gather(*(fetch(card) for card in missing))


def _local_catalogue_candidates(db, card_info, search_pairs) -> list:
    """Candidates from the locally synced catalogue, in the live search's shape.

    Only used when the remote catalogue could not be reached. Custom cards are
    excluded: they are this installation's own rows rather than catalogue
    entries, and matching a scan against one would invent a result the live
    search could never have produced.
    """
    # Each pair is searched as a pair. Splitting them into a list of names and
    # a list of languages would admit their product, so a name only ever
    # searched in English could match a row in another language. That is not
    # hypothetical: swsh2-201 is Milo in English and Yarrow in Italian, while
    # swsh7-201 is Milo in Italian, all numbered 201, so an Italian scan of
    # Yarrow would admit an unrelated Italian Milo through the English name.
    pairs = []
    for language, search_name in search_pairs:
        if search_name and (language, search_name) not in pairs:
            pairs.append((language, search_name))
    if not pairs:
        return []

    query = (
        db.query(Card)
        .filter(
            Card.tcg_card_id.isnot(None),
            Card.is_custom.isnot(True),
            or_(*[
                and_(Card.lang == language, Card.name.ilike(search_name))
                for language, search_name in pairs
            ]),
        )
    )

    # Narrow on the printed number in the query rather than truncating first.
    # An unordered LIMIT applied before this could drop the right printing while
    # leaving a different card of the same name and number in the result, which
    # the ranker then reads as a unique number and marks confident. That is a
    # wrong card filed automatically by "add all confident", which is worse than
    # no match at all.
    printed_number = card_info.get("number_local")
    number_forms = {
        form.casefold()
        for form in card_number_variants(printed_number)
    } if printed_number else set()
    if number_forms:
        query = query.filter(func.lower(Card.number).in_(sorted(number_forms)))

    # Ordered so the row set is deterministic: an arbitrary subset is what made
    # the truncation above dangerous, and an unordered LIMIT in PostgreSQL may
    # return a different 60 each time.
    rows = query.order_by(Card.lang.asc(), Card.set_id.asc(), Card.number.asc(), Card.id.asc()).limit(60).all()

    set_names = {}
    set_ids = {row.set_id for row in rows if row.set_id}
    if set_ids:
        for local_set in db.query(Set).filter(Set.tcg_set_id.in_(set_ids)).all():
            set_names.setdefault((local_set.tcg_set_id, local_set.lang), local_set.name)

    candidates = []
    for row in rows:
        language = row.lang or "en"
        candidates.append({
            "id": f"{row.tcg_card_id}_{language}",
            "tcg_card_id": row.tcg_card_id,
            "name": row.name,
            "set": set_names.get((row.set_id, language)),
            "number": row.number,
            "image": row.images_small or row.custom_image_url,
            "rarity": row.rarity,
            "lang": language,
            "_lang": language,
            "_number_extra": False,
            "_from_local_catalogue": True,
        })
    return candidates


def _catalogue_base_for(catalogue_name):
    """The base-URL resolver a candidate's own catalogue name refers to.

    Returns None when a standby-sourced candidate outlives its configuration,
    so a later enrichment silently skips rather than quietly asking a different
    catalogue about a card it may number differently.
    """
    if catalogue_name == CATALOGUE_STANDBY:
        return tcgdex_standby_base_url if tcgdex_standby_base_url() is not None else None
    return tcgdex_base_url


def _catalogue_bases():
    """The catalogues to search, in order of preference.

    The standby is only ever the second entry, and the caller stops after the
    first one that answers, so a configured standby costs nothing until the
    catalogue in front of it is unreachable.
    """
    yield CATALOGUE_PRIMARY, tcgdex_base_url
    if tcgdex_standby_base_url() is not None:
        yield CATALOGUE_STANDBY, tcgdex_standby_base_url


async def _search_one_catalogue(
    base_url,
    search_pairs,
    card_info: dict,
    trace: ScanTrace | None,
    request_gate,
    catalogue_name: str = CATALOGUE_PRIMARY,
) -> tuple[list[dict], int, int]:
    """Search one catalogue host for the given name and language pairs.

    Returns its candidates, how many lookups were made and how many of those
    failed to reach it. Extracted so the standby is searched by the same code
    as the primary rather than by a copy of it that can drift.
    """
    candidates = []
    attempted = 0
    unreachable = 0
    for search_language, search_name in search_pairs:
        if len(candidates) >= 15:
            break
        attempted += 1
        try:
            async with _request_gate(request_gate):
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(
                        f"{base_url(search_language)}/cards",
                        params={"name": search_name},
                    )
            # A 5xx is the catalogue failing, not an answer. So are 429 and 408:
            # the catalogue is declining to answer this request now, which is
            # not "no such card" either. Any other 4xx is an answer we should
            # not retry forever, so it is not counted as unreachable.
            if response.status_code >= 500 or response.status_code in {408, 429}:
                unreachable += 1
            cards = response.json() if response.status_code == 200 else []
            if trace:
                trace.record_tcgdex(
                    language=search_language,
                    query=search_name,
                    status=response.status_code,
                    count=len(cards) if isinstance(cards, list) else None,
                )
            if not isinstance(cards, list):
                continue
            selected_cards = select_search_candidates(
                cards,
                card_info.get("number_local"),
                number_field="localId",
            )
            for card in selected_cards:
                card_id = card.get("id")
                if not card_id:
                    continue
                candidates.append({
                    "id": f"{card_id}_{search_language}",
                    "tcg_card_id": card_id,
                    "name": card.get("name"),
                    "set": card.get("set", {}).get("name")
                    if isinstance(card.get("set"), dict) else None,
                    "number": card.get("localId"),
                    "image": f"{card.get('image')}/low.webp" if card.get("image") else None,
                    "rarity": card.get("rarity"),
                    "lang": search_language,
                    "_lang": search_language,
                    "_number_extra": bool(card.get("_number_extra")),
                    "_catalogue": catalogue_name,
                })
        except Exception as exc:
            if trace:
                trace.record_tcgdex(
                    language=search_language,
                    query=search_name,
                    status=None,
                    count=None,
                    error=type(exc).__name__,
                )
            unreachable += 1
            continue
    return candidates, attempted, unreachable


def _brief_set_id(card: dict) -> str:
    """The set a card brief belongs to, taken from its id.

    TCGdex card briefs carry only id, localId, name and image. There is no set
    object to read, and the API filters by containment, so a query for sv03
    also answers with sv03.5 and the two have to be told apart somehow. The id
    prefix is the only thing that does it: sv03-025 against sv03.5-025.
    """
    card_id = str(card.get("id") or "")
    return card_id.rsplit("-", 1)[0] if "-" in card_id else ""


def _partial_collector_number_pattern(value) -> re.Pattern | None:
    """Return a literal one-character-wildcard pattern for an uncertain number.

    Scanner extraction records an unreadable character as ``?``. This remains a
    retrieval-only hint: it is deliberately not accepted by the equality
    normaliser used by ranking and automatic decisions.
    """
    local = str(value or "").split("/", 1)[0].strip()
    if "?" not in local or not re.fullmatch(r"[A-Za-z0-9?]+", local):
        return None
    return re.compile("^" + re.escape(local).replace(r"\?", r"[A-Za-z0-9]") + "$", re.IGNORECASE)


def _has_usable_code_number(card_info: dict) -> bool:
    set_code = str(card_info.get("set_code") or "").strip()
    number = card_info.get("number_local")
    return bool(
        re.fullmatch(r"[A-Za-z0-9.]+", set_code)
        and (normalize_scanner_card_number(number) or _partial_collector_number_pattern(number))
    )


def _code_number_candidate(card: Card, set_row: Set | None, *, code: str) -> dict:
    language = card.lang or "en"
    return {
        "id": card.id,
        "tcg_card_id": card.tcg_card_id,
        "name": card.name,
        "set": set_row.name if set_row else None,
        "set_abbreviation": (set_row.abbreviation if set_row else code),
        "number": card.number,
        "image": card.images_small or card.custom_image_url,
        "rarity": card.rarity,
        "lang": language,
        "_lang": language,
        "_number_extra": False,
        "_from_local_catalogue": True,
        "_retrieved_by_code_number": True,
    }


def _local_code_number_candidates(db: Session, card_info: dict) -> tuple[list[dict], list[str]]:
    """Find a printing locally by set abbreviation, TCGdex id, or card set id."""
    set_code = str(card_info.get("set_code") or "").strip()
    if not _has_usable_code_number(card_info):
        return [], []
    lowered_code = set_code.casefold()
    set_rows = db.query(Set).filter(
        or_(
            func.lower(Set.abbreviation) == lowered_code,
            func.lower(Set.tcg_set_id) == lowered_code,
            func.lower(Set.id) == lowered_code,
        )
    ).all()
    set_ids = {row.tcg_set_id or row.id for row in set_rows}
    set_ids.update(
        row[0]
        for row in db.query(Card.set_id).filter(func.lower(Card.set_id) == lowered_code).all()
        if row[0]
    )
    if not set_ids:
        return [], []

    pattern = _partial_collector_number_pattern(card_info.get("number_local"))
    target = normalize_scanner_card_number(card_info.get("number_local"))
    language = normalize_tcgdex_language(card_info.get("language", "en"))
    rows = db.query(Card).filter(
        Card.set_id.in_(sorted(set_ids)),
        Card.tcg_card_id.isnot(None),
        Card.is_custom.isnot(True),
    ).all()
    matching_rows = [
        row for row in rows
        if (pattern and pattern.fullmatch(str(row.number or "")))
        or (target and normalize_scanner_card_number(row.number) == target)
    ]
    preferred_rows = [row for row in matching_rows if row.lang == language]
    if not preferred_rows and language != "en":
        preferred_rows = [row for row in matching_rows if row.lang == "en"]
    if preferred_rows:
        matching_rows = preferred_rows
    local_sets = {(row.tcg_set_id or row.id, row.lang): row for row in set_rows}
    return [
        _code_number_candidate(
            row, local_sets.get((row.set_id, row.lang)), code=set_code
        )
        for row in matching_rows
    ], sorted(set_ids)


async def _search_code_number_catalogue(
    base_url,
    set_ids: list[str],
    card_info: dict,
    trace: ScanTrace | None,
    request_gate,
    catalogue_name: str,
) -> tuple[list[dict], int, int]:
    """Retrieve a code-number printing from TCGdex, resolving an uncached code."""
    set_code = str(card_info.get("set_code") or "").strip()
    number = card_info.get("number_local")
    language = normalize_tcgdex_language(card_info.get("language", "en"))
    attempted = 0
    unreachable = 0
    resolved_ids = list(set_ids)
    try:
        if not resolved_ids:
            attempted += 1
            async with _request_gate(request_gate):
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(f"{base_url(language)}/sets")
            if response.status_code >= 500 or response.status_code in {408, 429}:
                unreachable += 1
            sets = response.json() if response.status_code == 200 else []
            if not isinstance(sets, list):
                sets = []
            # /sets returns Set Brief objects, which carry only id, name and
            # cardCount. There is no abbreviation to match on, and fetching
            # /sets/{id} for all ~218 of them to find one is not a lookup, it
            # is a crawl. So an uncached set resolves only when the printed
            # code IS its TCGdex id; a printed abbreviation that differs, as
            # OBF does from sv03, falls back to the name search. Locally
            # synced sets already resolve by abbreviation, which is the
            # ordinary case.
            for set_data in sets:
                if str(set_data.get("id") or "").casefold() == set_code.casefold():
                    set_id = set_data.get("id")
                    if set_id:
                        resolved_ids.append(str(set_id))
        candidates = []
        pattern = _partial_collector_number_pattern(number)
        for set_id in resolved_ids:
            attempted += 1
            params = {"set": set_id}
            if pattern is None:
                params["localId"] = str(number)
            async with _request_gate(request_gate):
                async with httpx.AsyncClient(timeout=15) as client:
                    response = await client.get(f"{base_url(language)}/cards", params=params)
            if response.status_code >= 500 or response.status_code in {408, 429}:
                unreachable += 1
            cards = response.json() if response.status_code == 200 else []
            if trace:
                trace.record_tcgdex(
                    language=language, query=f"{set_code} {number}",
                    status=response.status_code,
                    count=len(cards) if isinstance(cards, list) else None,
                )
            if not isinstance(cards, list):
                continue
            for card in cards:
                # TCGdex filters by containment, not equality, and the eq:
                # prefix returns nothing on this endpoint. A query for set
                # sv03 also answers with sv03.5, so the set has to be matched
                # exactly here. The collector number is already compared on
                # its normalised form below, which is what stops localId=25
                # also accepting 125.
                # A card brief carries only id, localId, name and image - no
                # set object - so the set comes from the id's own prefix:
                # sv03-025 is sv03, and sv03.5-025 is sv03.5. Reading a set
                # object that is not there discarded every remote result,
                # including the one being asked for.
                if _brief_set_id(card).casefold() != str(set_id).casefold():
                    continue
                local_id = str(card.get("localId") or "")
                if pattern and not pattern.fullmatch(local_id):
                    continue
                if not pattern and normalize_scanner_card_number(local_id) != normalize_scanner_card_number(number):
                    continue
                card_id = card.get("id")
                if not card_id:
                    continue
                candidates.append({
                    "id": f"{card_id}_{language}",
                    "tcg_card_id": card_id,
                    "name": card.get("name"),
                    "set": card.get("set", {}).get("name") if isinstance(card.get("set"), dict) else None,
                    "set_abbreviation": set_code,
                    "number": card.get("localId"),
                    "image": f"{card.get('image')}/low.webp" if card.get("image") else None,
                    "rarity": card.get("rarity"),
                    "lang": language,
                    "_lang": language,
                    "_number_extra": False,
                    "_catalogue": catalogue_name,
                    "_retrieved_by_code_number": True,
                })
        return candidates, attempted, unreachable
    except Exception as exc:
        if trace:
            trace.record_tcgdex(
                language=language, query=f"{set_code} {number}", status=None,
                count=None, error=type(exc).__name__,
            )
        return [], attempted or 1, unreachable + 1


async def _search_and_rank_candidates(
    db: Session,
    card_info: dict,
    trace: ScanTrace | None = None,
    *,
    request_gate=None,
) -> tuple[list[dict], int]:
    card_name = str(card_info.get("name") or "").strip()
    card_name_en = str(card_info.get("name_en") or card_name).strip() or card_name
    has_code_number = _has_usable_code_number(card_info)
    if not card_name and not has_code_number:
        raise HTTPException(
            status_code=422,
            detail=(
                "Neither the card name nor a readable set code and collector number "
                "could be read from this photo."
            ),
        )

    simple_name = _simplify_name(card_name)
    simple_name_en = _simplify_name(card_name_en)
    language = normalize_tcgdex_language(card_info.get("language", "en"))
    if not is_supported_tcgdex_language(language):
        language = "en"
    search_pairs = []
    if card_name:
        search_pairs = [(language, simple_name)]
        if simple_name != card_name:
            search_pairs.append((language, card_name))
        if language != "en":
            search_pairs.append(("en", simple_name_en))
            if simple_name_en != card_name_en:
                search_pairs.append(("en", card_name_en))

    candidates, set_ids = _local_code_number_candidates(db, card_info)
    attempted = 0
    unreachable = 0
    if not candidates and has_code_number:
        for catalogue_name, base_url in _catalogue_bases():
            found, base_attempted, base_unreachable = await _search_code_number_catalogue(
                base_url, set_ids, card_info, trace, request_gate, catalogue_name
            )
            candidates.extend(found)
            attempted += base_attempted
            unreachable += base_unreachable
            if not (base_attempted and base_unreachable == base_attempted):
                break
    if not candidates and search_pairs:
        for catalogue_name, base_url in _catalogue_bases():
            found, base_attempted, base_unreachable = await _search_one_catalogue(
                base_url, search_pairs, card_info, trace, request_gate, catalogue_name
            )
            candidates.extend(found)
            attempted += base_attempted
            unreachable += base_unreachable
            # Move on to the standby only when this catalogue could not be reached
            # at all. If it answered, its answer stands, including an answer of
            # "no such card": a second opinion sought only because the first was
            # not to our liking is not a fallback, it is a coin toss.
            if not (base_attempted and base_unreachable == base_attempted):
                break

    if not candidates and attempted and unreachable == attempted:
        # The catalogue is unreachable, but this installation already holds a
        # synced copy of it. Searching that is not as good as a live lookup, it
        # only knows the sets that have been synced, but it is far better than
        # telling the user their card does not exist because a remote host is
        # down. Images come from the synced rows, so they are exactly as
        # available as they were before.
        candidates.extend(_local_catalogue_candidates(db, card_info, search_pairs))

    if not candidates and attempted and unreachable == attempted:
        # Every lookup failed to reach the catalogue, so we do not know whether
        # this card exists. Saying "no matches" here is a lie that looks like a
        # missing card, and it sent at least one user hunting through their own
        # install during an upstream outage. A 503 is retried with backoff, so
        # the scan recovers on its own once the catalogue is reachable again.
        raise CatalogueUnreachableHTTPException(
            "The card catalogue could not be reached, so this photo has not "
            "been matched yet."
        )

    candidate_set_ids = {
        tcg_card_id.rsplit("-", 1)[0]
        for candidate in candidates
        if "-" in (tcg_card_id := candidate.get("tcg_card_id", ""))
    }
    local_sets = {}
    if candidate_set_ids:
        # As in _fill_candidate_details: query and read back with no await in
        # between, so a shared Session is never re-entered mid-transaction.
        rows = db.query(Set).filter(Set.tcg_set_id.in_(candidate_set_ids)).all()
        local_sets = {(row.tcg_set_id, row.lang): row for row in rows}
    for candidate in candidates:
        tcg_card_id = candidate.get("tcg_card_id", "")
        if "-" not in tcg_card_id:
            continue
        set_id = tcg_card_id.rsplit("-", 1)[0]
        language = candidate.get("_lang", "en")
        local_set = local_sets.get((set_id, language))
        if local_set:
            candidate["set"] = local_set.name
            candidate["set_abbreviation"] = local_set.abbreviation
            candidate["printed_total"] = local_set.printed_total or None

    seen = set()
    deduped = []
    for candidate in candidates:
        key = (candidate.get("id"), candidate.get("_lang", "en"))
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)

    await _fill_candidate_details(db, deduped, card_info, request_gate=request_gate)
    deduped.sort(key=lambda card: _candidate_rank_key(card_info, card))
    number_match_count = sum(
        1
        for card in deduped
        if _identity_signal(card_info.get("number_local"), card.get("number"), _numbers_match) == 0
    )
    if trace:
        trace.record_prefilter(
            card_info.get("number_local"),
            number_match_count,
            len(deduped),
        )
        trace.record_candidates(deduped, lambda card: _candidate_rank_key(card_info, card))
    return deduped, number_match_count


async def match_card_info(
    db: Session,
    card_info: dict,
    *,
    api_key: str | None = None,
    image_b64: str | None = None,
    mime_type: str | None = None,
    allow_visual_verification: bool = False,
    photo_bytes: bytes | None = None,
    trace: ScanTrace | None = None,
    provider: ScanProvider | None = None,
    request_gate=None,
) -> dict:
    """Shared deterministic matcher for both individual and composite scans.

    provider defaults to Gemini so existing callers, including the tests, behave
    exactly as before.

    request_gate is an optional async context manager entered around each
    outbound TCGdex request. A caller running several matchers at once passes a
    shared bounded gate so the group's burst stays at one matcher's peak;
    callers that pass nothing are gated not at all, exactly as before.

    db is used for two short synchronous reads, each issued and consumed without
    an await in between. That is what makes it safe to hand several concurrent
    matchers the same Session rather than one connection each.
    """
    provider = provider or ScanProvider(GEMINI)
    card_info = normalize_recognized_card_info(card_info)
    candidates, number_match_count = await _search_and_rank_candidates(
        db, card_info, trace, request_gate=request_gate
    )
    confident, decision = _metadata_decision(card_info, candidates)
    if (
        confident
        and candidates
        and candidates[0].get("_retrieved_by_code_number")
        and not _code_number_name_agrees(card_info, candidates[0])
    ):
        # A code and complete number retrieve the printing, but a different
        # readable name means the extraction contradicts it. Keep the result
        # visible for review and do not silently file either interpretation.
        confident = False
        decision = "code_number_name_disagrees"

    top_candidates = retain_ranked_candidates(candidates)
    can_compare = sum(1 for card in top_candidates if card.get("image")) >= 2
    if photo_bytes is None and image_b64:
        try:
            photo_bytes = base64.b64decode(image_b64, validate=True)
        except (ValueError, TypeError):
            photo_bytes = None

    should_try_phash = not confident and can_compare and bool(photo_bytes)
    should_try_visual = (
        allow_visual_verification
        and not confident
        and can_compare
        # A credential is required only where the provider requires one. A local
        # endpoint has no key by design, so testing the key here would silently
        # disable this for exactly the setups the toggle exists for.
        and (bool(api_key) or not provider.requires_credential())
        and bool(image_b64)
        and bool(mime_type)
    )
    if should_try_phash or should_try_visual:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                candidate_images = await _download_candidate_images(
                    client,
                    top_candidates[:PHASH_CANDIDATE_LIMIT],
                    request_gate=request_gate,
                )
                if should_try_phash:
                    try:
                        winner = _phash_best_match(
                            top_candidates,
                            photo_bytes,
                            candidate_images,
                            trace,
                        )
                        if (
                            winner is not None
                            and 2 not in _candidate_rank_key(card_info, winner)
                        ):
                            candidates.remove(winner)
                            candidates.insert(0, winner)
                            confident = True
                            decision = "phash"
                        elif winner is not None and trace:
                            trace.reject_phash("metadata_contradiction")
                    except Exception as exc:
                        logger.warning("pHash matching failed (non-blocking): %s", exc)

                if not confident and should_try_visual:
                    candidate_images = await _download_candidate_images(
                        client,
                        top_candidates,
                        candidate_images,
                        request_gate=request_gate,
                    )
                    parts = [
                        {"text": "Here is the original card photo:"},
                        image_part(mime_type, image_b64),
                        {"text": (
                            "Choose the matching candidate using artwork and printed "
                            "identity details. Respond with only its number, or 0 if "
                            "none match.\n"
                        )},
                    ]
                    for index, candidate in enumerate(top_candidates, start=1):
                        parts.append({"text": (
                            f"\nCandidate {index}: {candidate.get('name', '?')} "
                            f"#{candidate.get('number', '?')}"
                        )})
                        candidate_bytes = candidate_images.get(
                            str(candidate.get("id") or "")
                        )
                        if candidate_bytes:
                            parts.append(image_part_from_bytes("image/webp", candidate_bytes))
                        else:
                            parts.append({"text": " (image unavailable)"})

                    response_text, _visual_usage = await provider.generate_text(
                        client,
                        api_key,
                        parts,
                        max_attempts=2,
                    )
                    pick_match = re.search(r"(\d+)", response_text)
                    selected_position = int(pick_match.group(1)) if pick_match else None
                    if trace:
                        trace.record_visual_verification(
                            raw_response=response_text,
                            selected=selected_position,
                        )
                    if pick_match:
                        pick = selected_position
                        if 1 <= pick <= len(top_candidates):
                            winner = top_candidates[pick - 1]
                            candidates.remove(winner)
                            candidates.insert(0, winner)
                            confident = True
                            # Unchanged for Gemini: this value is persisted in
                            # diagnostics and asserted by the existing tests.
                            decision = (
                                "gemini_visual" if provider.is_gemini
                                else f"{provider.name}_visual"
                            )
        except Exception as exc:
            logger.warning("Visual matching failed (non-blocking): %s", exc)

    public_matches = [
        {
            key: value
            for key, value in card.items()
            if key not in {"_number_extra", "_retrieved_by_code_number"}
        }
        for card in retain_ranked_candidates(candidates)
    ]
    # candidates[0] is the matcher's pick: the ranked winner, or whichever
    # candidate pHash or visual verification promoted to the front. Naming it
    # here rather than only inside the trace is what lets a caller persist the
    # decision instead of re-deriving it from list order, which stops meaning
    # "chosen" the moment the matcher is not confident.
    chosen = candidates[0] if confident and candidates else None
    # Diagnostics name the card; the review UI has to name the *candidate*, and
    # the two are not interchangeable. One tcg_card_id can legitimately appear
    # several times in a single candidate list - once per language searched -
    # because the searches above are per language and the dedup below keys on
    # the per-language id, not on the card. Handing the review grid a
    # tcg_card_id therefore marks every language copy of that card as "the"
    # suggestion at once. The per-language id is unique within the list by
    # construction, so it is the only handle that can single one candidate out.
    selected_card_id = (str(chosen.get("tcg_card_id") or "") or None) if chosen else None
    selected_match_id = (str(chosen.get("id") or "") or None) if chosen else None
    if trace:
        trace.record_decision(decision or "undecided", selected_card_id)
    return {
        "recognized": card_info,
        "matches": public_matches,
        "_number_match_count": number_match_count,
        "_identity_confident": confident,
        "_identity_decision": decision,
        # matches[].id, not matches[].tcg_card_id. See above.
        "_identity_suggested_match_id": selected_match_id,
    }


async def recognize_sanitized_card(
    db: Session,
    user_id: int,
    image_bytes: bytes,
    content_type: str,
    *,
    trace: ScanTrace | None = None,
) -> dict:
    """Recognize one already-sanitized image for direct and queued scans."""
    provider = get_provider(db, user_id)
    api_key = provider.credential(db, user_id)
    if provider.requires_credential() and not api_key:
        if trace:
            trace.record_error(f"No {provider.name} API key is configured.")
        raise HTTPException(
            status_code=400,
            detail=provider.missing_credential_message(),
        )

    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=recognition_timeout()) as client:
            response_text, usage = await provider.generate_text(
                client,
                api_key,
                [text_part(RECOGNIZE_PROMPT), image_part(content_type, image_b64)],
            )
        response_text = response_text.strip()
        if trace:
            trace.record_extraction(
                prompt=RECOGNIZE_PROMPT,
                raw_response=response_text,
                usage=usage,
            )
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON found in the scanner response")
        card_info = normalize_recognized_card_info(json.loads(json_match.group()))
        if trace:
            trace.record_extraction(parsed=card_info)
    except HTTPException as exc:
        if trace:
            trace.record_error(str(exc.detail))
        raise
    except Exception as exc:
        if trace:
            trace.record_error(f"Recognition parsing failed: {type(exc).__name__}")
        raise HTTPException(status_code=500, detail=f"Recognition failed: {exc}")

    try:
        return await match_card_info(
            db,
            card_info,
            api_key=api_key,
            image_b64=image_b64,
            mime_type=content_type,
            allow_visual_verification=visual_verification_enabled(
                db, user_id, provider.name
            ),
            photo_bytes=image_bytes,
            trace=trace,
            provider=provider,
        )
    except HTTPException as exc:
        if trace:
            trace.record_error(str(exc.detail))
        raise
    except Exception as exc:
        if trace:
            trace.record_error(f"Candidate matching failed: {type(exc).__name__}")
        raise


@router.post("/recognize")
async def recognize_card(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        raw_image = await read_limited_upload(file, remaining_job_bytes=MAX_FILE_BYTES)
        sanitized = sanitize_image_bytes(raw_image)
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    trace = create_scan_trace(
        db,
        current_user.id,
        mode="single",
        filename="sanitized-upload.jpg",
        # Stamped with whichever provider will actually run, so diagnostics do not
        # attribute an OpenAI scan to the Gemini model.
        model=get_provider(db, current_user.id).model(),
    )
    trace.set_image(sanitized.data)
    try:
        return await recognize_sanitized_card(
            db,
            current_user.id,
            sanitized.data,
            sanitized.content_type,
            trace=trace,
        )
    finally:
        trace.save()


COMPOSITE_PROMPT = """This image contains {count} separate Pokemon Trading Card Game cards.
They are arranged left-to-right, then top-to-bottom, and each card has a white index number
on a black square directly above it. Identify every card. Read that index label instead of
relying on response order.

For each card return the same information as an individual scan:
- index: the printed corner number
- set_code: printed alphanumeric set code near the number, or null
- number_local: printed local collector number, a partial pattern using `?`, or null
- number_total: printed set total/denominator, or null
- name: exact card name in the card's language, for confirmation
- name_en: English card name, for confirmation
- regulation_mark: boxed regulation letter, or null
- card_type: Pokemon, Trainer, or Energy
- hp: HP value or null
- language: two-letter ISO language code
- artist: printed illustrator credit, or null

Read the set code and collector number first. Together they identify the printing; use the
name, artwork and other details only as confirmation. Never infer set_code from the artwork
or from recognizing the set. For number_local only, write each unreadable but positioned
character as `?`, such as `2?5`; use null when it is absent or no useful pattern is visible.
For all other small identity text, use null rather than guessing if any character is unclear.
Respond ONLY with a JSON array containing one object per card, without markdown or explanation.
"""


class CompositeRecognitionError(ValueError):
    """The composite response could not be mapped safely to its source photos."""


async def recognize_composite_card_info(
    api_key: str,
    image_bytes: bytes,
    count: int,
    *,
    traces: list[ScanTrace] | None = None,
    provider: ScanProvider | None = None,
) -> dict[int, dict]:
    """Return recognized card information keyed by zero-based composite position."""
    provider = provider or ScanProvider(GEMINI)
    image_b64 = base64.b64encode(image_bytes).decode()
    try:
        async with httpx.AsyncClient(timeout=recognition_timeout()) as client:
            response_text, usage = await provider.generate_text(
                client,
                api_key,
                [
                    text_part(COMPOSITE_PROMPT.format(count=count)),
                    image_part("image/jpeg", image_b64),
                ],
            )
        response_text = response_text.strip()
        for trace in traces or []:
            trace.record_extraction(
                prompt=COMPOSITE_PROMPT.format(count=count),
                raw_response=response_text,
                usage=usage,
            )
        array_match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if not array_match:
            raise CompositeRecognitionError("The scanner returned no card list for the composite.")
        rows = json.loads(array_match.group())
        if not isinstance(rows, list):
            raise CompositeRecognitionError("The scanner returned an invalid composite card list.")
    except HTTPException:
        raise
    except CompositeRecognitionError:
        raise
    except Exception as exc:
        raise CompositeRecognitionError(f"Could not parse the composite result: {exc}") from exc

    mapped: dict[int, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            position = int(row.get("index")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= position < count and position not in mapped:
            mapped[position] = normalize_recognized_card_info(row)
            if traces and position < len(traces):
                traces[position].record_extraction(parsed=mapped[position])
    return mapped


async def match_composite_card_info(
    db: Session,
    card_info: dict,
    *,
    photo_bytes: bytes | None = None,
    trace: ScanTrace | None = None,
    request_gate=None,
) -> dict:
    """Use local pHash before an uncertain composite falls back individually.

    allow_visual_verification stays False, so this path never reaches a paid
    vision provider: the one composite extraction per claim is the only such
    call, and running these concurrently cannot add another.
    """
    return await match_card_info(
        db,
        card_info,
        allow_visual_verification=False,
        photo_bytes=photo_bytes,
        trace=trace,
        request_gate=request_gate,
    )
