"""Vision providers for the card scanner.

Gemini stays the default and its request path is untouched: this module calls the
existing post_gemini_generate() rather than reimplementing it, so Gemini keeps its
own retry, rate limiting and quota handling exactly as before.

The second provider speaks the OpenAI chat-completions API, which covers hosted
OpenAI and any compatible local server (Ollama, llama.cpp, LM Studio).

Two rules shape the design:

- The base URL is read from the environment only. A user-supplied backend URL would
  let any account point the server at an arbitrary host, which is a server-side
  request forgery. Administrators configure the endpoint; users only choose between
  the providers the administrator has made available.
- The Gemini rate limiter is keyed by a fingerprint of the API key. A local endpoint
  usually has no key, so every account would share one bucket and one user's penalty
  would block everybody. Non-Gemini providers therefore never enter that limiter.
"""

import base64
import json
import logging
import os
import re
from contextlib import nullcontext

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import UserSetting

logger = logging.getLogger(__name__)

GEMINI = "gemini"
OPENAI = "openai"
PROVIDERS = (GEMINI, OPENAI)

SCANNER_PROVIDER_SETTING = "scanner_provider"
VISUAL_VERIFICATION_SETTING = "scanner_visual_verification"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# Retried rather than surfaced: the same classes Gemini already retries.
OPENAI_TRANSIENT_STATUS_CODES = {502, 503, 504}


def openai_base_url() -> str:
    """Where OpenAI-compatible requests go, set by the administrator.

    Stripped before the fallback, because a value of only whitespace is truthy and
    would otherwise pass through as an empty base URL.
    """
    configured = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    return configured or DEFAULT_OPENAI_BASE_URL


def openai_chat_completions_url() -> str:
    return f"{openai_base_url()}/chat/completions"


def openai_model() -> str:
    return (os.environ.get("OPENAI_MODEL") or "").strip() or DEFAULT_OPENAI_MODEL


def openai_requires_key() -> bool:
    """Only the hosted API needs a credential.

    Pointing OPENAI_BASE_URL at a local server means there is usually nothing to
    authenticate against, and demanding a key there would make local use impossible.
    """
    return openai_base_url() == DEFAULT_OPENAI_BASE_URL


def resolve_provider_name(db: Session, user_id: int | None) -> str:
    """Which provider this user scans with, defaulting to Gemini.

    An unrecognised stored value falls back rather than raising: settings validation
    rejects bad input at write time, and a scan is the wrong place to fail over a
    configuration typo.
    """
    if user_id is None:
        return GEMINI
    row = (
        db.query(UserSetting)
        .filter(UserSetting.user_id == user_id, UserSetting.key == SCANNER_PROVIDER_SETTING)
        .first()
    )
    value = (row.value if row else "") or ""
    value = value.strip().lower()
    return value if value in PROVIDERS else GEMINI


def visual_verification_enabled(db: Session, user_id: int | None, provider: str) -> bool:
    """Whether to spend a second model call on picking between candidates.

    Defaults differ by provider on purpose. Gemini handles the multi-image
    comparison well, so it stays on and nothing changes for existing users. A small
    local model usually does not, and a wrong pick is worse than no pick, so it
    starts off there. Either default can be overridden per user.
    """
    if user_id is not None:
        row = (
            db.query(UserSetting)
            .filter(
                UserSetting.user_id == user_id,
                UserSetting.key == VISUAL_VERIFICATION_SETTING,
            )
            .first()
        )
        if row and row.value:
            return str(row.value).strip().lower() in {"true", "1", "yes", "on"}
    return provider == GEMINI


class ProviderRateLimitError(HTTPException):
    """A 429 carrying the retry metadata scan_queue reads off the exception.

    scan_queue._scan_error_from_http() pulls retry_after_seconds and
    retry_reason by getattr, so a plain HTTPException would be retried with no
    backoff at all.
    """

    def __init__(self, *, retry_after_seconds: float | None, detail: str):
        self.retry_after_seconds = float(retry_after_seconds or 0.0)
        self.retry_reason = "rate_limit"
        headers = None
        if retry_after_seconds:
            headers = {"Retry-After": str(max(1, int(self.retry_after_seconds + 0.999)))}
        super().__init__(status_code=429, detail=detail, headers=headers)


def safe_upstream_detail(resp: httpx.Response, *, limit: int = 200) -> str:
    """Upstream error text, redacted and bounded.

    Provider messages are echoed into API responses, persisted as queue-item
    errors and logged. A compatible endpoint that reports "Invalid API key:
    <secret>" would otherwise write that secret to all three.
    """
    from services.scan_trace import redact_sensitive

    message = redact_sensitive(openai_error_message(resp)).strip()
    return message[:limit]


def openai_error_message(resp: httpx.Response) -> str:
    """Best-effort upstream detail, without assuming the error envelope."""
    try:
        payload = resp.json()
    except Exception:
        return ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or "").strip()
        if isinstance(error, str):
            return error.strip()
        if payload.get("message"):
            return str(payload["message"]).strip()
    return ""


def openai_retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _openai_content(parts: list[dict]) -> list[dict]:
    """Turn neutral parts into OpenAI content blocks.

    Neutral part shapes, shared with the Gemini serialiser:
        {"text": "..."}
        {"image": {"mime_type": "image/jpeg", "data": "<base64>"}}
    """
    content = []
    for part in parts:
        if "text" in part:
            content.append({"type": "text", "text": part["text"]})
        elif "image" in part:
            image = part["image"]
            mime = image.get("mime_type") or "image/jpeg"
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{image['data']}"},
            })
    return content


def _gemini_parts(parts: list[dict]) -> list[dict]:
    """Turn neutral parts into Gemini parts, the shape it already expects."""
    converted = []
    for part in parts:
        if "text" in part:
            converted.append({"text": part["text"]})
        elif "image" in part:
            image = part["image"]
            converted.append({"inline_data": {
                "mime_type": image.get("mime_type") or "image/jpeg",
                "data": image["data"],
            }})
    return converted


def image_part(mime_type: str | None, data_b64: str) -> dict:
    return {"image": {"mime_type": mime_type or "image/jpeg", "data": data_b64}}


def text_part(text: str) -> dict:
    return {"text": text}


def image_part_from_bytes(mime_type: str | None, raw: bytes) -> dict:
    return image_part(mime_type, base64.b64encode(raw).decode())


async def post_openai_chat(
    client: httpx.AsyncClient,
    url: str,
    api_key: str,
    payload: dict,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    """Call an OpenAI-compatible endpoint, retrying the same transient classes.

    Deliberately not routed through the Gemini rate limiter: see the module
    docstring. A local endpoint has no shared quota to protect.
    """
    import asyncio

    last_error = None
    for attempt in range(max_attempts):
        try:
            headers = {"Content-Type": "application/json"}
            # Sent only when there is one. A local server rejects, or ignores, an
            # Authorization header carrying an empty bearer token.
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code == 429:
                detail = "The scanner provider is rate limited. Please try again shortly."
                upstream = safe_upstream_detail(resp)
                if upstream:
                    detail = f"{detail} Provider reports: {upstream}"
                raise ProviderRateLimitError(
                    retry_after_seconds=openai_retry_after_seconds(resp),
                    detail=detail,
                )
            if resp.status_code in {400, 401, 403}:
                # Deliberately no upstream text on the authentication classes.
                # This is exactly where endpoints quote the offending credential
                # back, and this detail is persisted and logged downstream.
                if openai_requires_key():
                    detail = "The OpenAI API key was rejected. Please check it in Settings."
                else:
                    detail = "The scanner endpoint rejected the request."
                raise HTTPException(status_code=400, detail=detail)
            if resp.status_code == 404:
                upstream = safe_upstream_detail(resp)
                detail = (
                    "The scanner model was not found at this endpoint. "
                    "Check OPENAI_MODEL and OPENAI_BASE_URL."
                )
                if upstream:
                    detail = f"{detail} Provider reports: {upstream}"
                raise HTTPException(status_code=502, detail=detail)
            if resp.status_code in OPENAI_TRANSIENT_STATUS_CODES:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise HTTPException(
                    status_code=503,
                    detail="The scanner provider is temporarily unavailable. Please try again shortly.",
                )
            if resp.is_error:
                upstream = safe_upstream_detail(resp)
                detail = f"The scanner request failed ({resp.status_code})."
                if upstream:
                    detail = f"{detail} Provider reports: {upstream}"
                raise HTTPException(status_code=502, detail=detail)
            return resp
        except HTTPException:
            raise
        except httpx.RequestError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise HTTPException(
                status_code=503,
                detail="The scanner endpoint could not be reached. Check the connection and try again.",
            )

    raise HTTPException(status_code=500, detail=f"The scanner request failed: {last_error}")


def extract_openai_text(payload: dict) -> str:
    """Pull the assistant message out of a chat-completions response."""
    try:
        # Not stripped here, to match the Gemini adapter: call sites decide.
        return payload["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("No message content in the scanner response") from exc


class ScanProvider:
    """One provider's calling convention, so call sites stay provider-agnostic."""

    def __init__(self, name: str):
        self.name = name

    @property
    def is_gemini(self) -> bool:
        return self.name == GEMINI

    def model(self) -> str:
        from api.recognize import get_gemini_model

        return get_gemini_model() if self.is_gemini else openai_model()

    def credential(self, db: Session, user_id: int | None) -> str:
        from api.recognize import get_gemini_key

        if self.is_gemini:
            return get_gemini_key(db, user_id=user_id)
        if user_id is None:
            return ""
        row = (
            db.query(UserSetting)
            .filter(UserSetting.user_id == user_id, UserSetting.key == "openai_api_key")
            .first()
        )
        return (row.value if row else "") or ""

    def requires_credential(self) -> bool:
        return True if self.is_gemini else openai_requires_key()

    def missing_credential_message(self) -> str:
        if self.is_gemini:
            return "Kein Gemini API Key konfiguriert. Bitte in den Einstellungen eintragen."
        return "No OpenAI API key configured. Add one in Settings first."

    def rate_limit_scope(self, priority: str):
        """Gemini's queue priority scope, and nothing for other providers."""
        if self.is_gemini:
            from services.gemini_rate_limit import gemini_priority_scope

            return gemini_priority_scope(priority)
        return nullcontext()

    async def generate_text(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        parts: list[dict],
        *,
        max_attempts: int = 3,
    ) -> tuple[str, dict | None]:
        """Run one multimodal request and return (text, usage).

        Usage is whatever the provider reports, or None. Callers record it for
        diagnostics and must not depend on its shape.
        """
        if self.is_gemini:
            from api.recognize import build_gemini_generate_url, post_gemini_generate

            response = await post_gemini_generate(
                client,
                build_gemini_generate_url(),
                api_key,
                {"contents": [{"parts": _gemini_parts(parts)}]},
                max_attempts=max_attempts,
            )
            payload = response.json()
            # Returned exactly as received. Upstream stripped at the extraction
            # and composite call sites but recorded visual verification
            # unstripped, so stripping here would change what Gemini writes into
            # diagnostics. Call sites strip where they always did.
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
            return text, payload.get("usageMetadata")

        response = await post_openai_chat(
            client,
            openai_chat_completions_url(),
            api_key,
            {
                "model": self.model(),
                "messages": [{"role": "user", "content": _openai_content(parts)}],
            },
            max_attempts=max_attempts,
        )
        payload = response.json()
        return extract_openai_text(payload), payload.get("usage")


def get_provider(db: Session, user_id: int | None) -> ScanProvider:
    return ScanProvider(resolve_provider_name(db, user_id))
