"""Pluggable vision providers for the card scanner.

Upstream PokeCollector talks to Google Gemini directly from ``api/recognize.py``.
This module lifts the provider-specific wire format behind a small interface so
the same recognition flow can run against OpenAI instead.

Gemini behaviour is deliberately unchanged: :class:`GeminiVisionProvider` is a
straight lift of the original request/response handling, and the module-level
Gemini helpers keep their original names and signatures so existing callers and
tests continue to work.

Providers exchange a neutral "parts" list built with :func:`text_part` and
:func:`image_part`; each provider converts that into its own payload shape and
returns the model's raw text response.
"""

import asyncio
import os

import httpx
from fastapi import HTTPException

TRANSIENT_STATUS_CODES = {502, 503, 504}

DEFAULT_GEMINI_MODEL = "gemini-flash-latest"
GEMINI_MODELS_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def openai_base_url() -> str:
    """Where the OpenAI-compatible API lives.

    Ollama, llama.cpp and LM Studio all serve the same chat completions shape,
    so pointing this elsewhere is the whole of what is needed to run the scanner
    against a local model.
    """
    configured = (os.environ.get("OPENAI_BASE_URL") or "").strip().rstrip("/")
    return configured or DEFAULT_OPENAI_BASE_URL


def openai_chat_completions_url() -> str:
    return f"{openai_base_url()}/chat/completions"

DEFAULT_PROVIDER = "gemini"


def shared_scanner_key_allowed() -> bool:
    """Whether an installation-wide key may be used for any account.

    Off by default: the key belongs to whoever pays for the deployment, so
    sharing it with every authenticated account is a billing and isolation
    decision the operator has to make deliberately. With it off, behaviour
    matches the per-user model the project documents.
    """
    return os.environ.get("ALLOW_SHARED_SCANNER_KEY", "").strip().lower() in {
        "true", "1", "yes", "on",
    }


# --------------------------------------------------------------------------
# Neutral message parts
# --------------------------------------------------------------------------

def text_part(text: str) -> dict:
    """A text fragment in a provider-neutral prompt."""
    return {"type": "text", "text": text}


def image_part(mime_type: str, data_b64: str) -> dict:
    """A base64 image fragment in a provider-neutral prompt."""
    return {"type": "image", "mime_type": mime_type, "data": data_b64}


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def upstream_error_message(resp: httpx.Response) -> str:
    """Extract the useful upstream error body when available.

    Gemini and OpenAI both return ``{"error": {"message": ...}}``.
    """
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()
    error = data.get("error") if isinstance(data, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return str(message or "").strip()


# Kept under the original name for backwards compatibility.
gemini_error_message = upstream_error_message


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


def get_openai_model() -> str:
    """Return the configured OpenAI model name."""
    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()
    return model or DEFAULT_OPENAI_MODEL


async def _post_with_retries(
    client,
    url: str,
    headers: dict,
    payload: dict,
    *,
    label: str,
    vendor: str,
    model_hint: str,
    max_attempts: int = 3,
) -> httpx.Response:
    """POST to a vision API, mapping upstream failures onto useful HTTP errors.

    Retries only the transient capacity codes; auth and model errors fail fast
    because retrying them cannot succeed.
    """
    last_error = None

    for attempt in range(max_attempts):
        try:
            resp = await client.post(url, headers=headers, json=payload)

            if resp.status_code == 429:
                raise HTTPException(
                    status_code=429,
                    detail=f"{label} Rate Limit erreicht – bitte kurz warten und nochmal versuchen.",
                )
            if resp.status_code in {400, 401, 403}:
                raise HTTPException(
                    status_code=400,
                    detail=f"Ungültiger {label} API Key. Bitte in den Einstellungen prüfen.",
                )
            if resp.status_code == 404:
                upstream_message = upstream_error_message(resp)
                detail = (
                    f"{label} Modell nicht verfügbar. "
                    f"Bitte {model_hint} auf ein unterstütztes Modell setzen."
                )
                if upstream_message:
                    detail = f"{detail} {vendor} meldet: {upstream_message}"
                raise HTTPException(status_code=502, detail=detail)
            if resp.status_code in TRANSIENT_STATUS_CODES:
                if attempt < max_attempts - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"{label} ist gerade temporär überlastet oder nicht verfügbar. "
                        "Bitte gleich nochmal versuchen."
                    ),
                )
            if resp.is_error:
                upstream_message = upstream_error_message(resp)
                detail = f"{label} Anfrage fehlgeschlagen ({resp.status_code})."
                if upstream_message:
                    detail = f"{detail} {vendor} meldet: {upstream_message}"
                raise HTTPException(status_code=502, detail=detail)
            return resp
        except HTTPException:
            raise
        except httpx.RequestError as e:
            last_error = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            raise HTTPException(
                status_code=503,
                detail=(
                    f"{label} konnte gerade nicht erreicht werden. "
                    "Bitte Verbindung prüfen oder später erneut versuchen."
                ),
            )

    raise HTTPException(status_code=500, detail=f"{label} Anfrage fehlgeschlagen: {last_error}")


async def post_gemini_generate(
    client,
    gemini_url: str,
    api_key: str,
    payload: dict,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    """Call Gemini with small retries for transient capacity errors."""
    return await _post_with_retries(
        client,
        gemini_url,
        {"x-goog-api-key": api_key},
        payload,
        label="Gemini",
        vendor="Google",
        model_hint="GEMINI_MODEL",
        max_attempts=max_attempts,
    )


async def post_openai_chat(
    client,
    api_key: str,
    payload: dict,
    *,
    max_attempts: int = 3,
) -> httpx.Response:
    """Call the OpenAI chat completions API with the same retry semantics."""
    # A self-hosted endpoint usually wants no key at all, so only send the
    # header when there is something to send.
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await _post_with_retries(
        client,
        openai_chat_completions_url(),
        headers,
        payload,
        label="OpenAI",
        vendor="OpenAI",
        model_hint="OPENAI_MODEL",
        max_attempts=max_attempts,
    )


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class VisionProvider:
    """Common interface for a vision model that can read a card photo."""

    name = ""
    #: per-user setting key holding this provider's API key
    settings_key = ""
    #: environment variable consulted when the user has no key of their own
    env_key = ""
    #: human-readable name used in error messages
    label = ""

    def model(self) -> str:
        raise NotImplementedError

    def build_payload(self, parts: list[dict]) -> dict:
        raise NotImplementedError

    def extract_text(self, data: dict) -> str:
        raise NotImplementedError

    async def post(self, client, api_key: str, payload: dict, *, max_attempts: int = 3):
        raise NotImplementedError

    async def generate(
        self,
        client,
        api_key: str,
        parts: list[dict],
        *,
        max_attempts: int = 3,
    ) -> str:
        """Send neutral parts to the provider and return its raw text response."""
        resp = await self.post(
            client, api_key, self.build_payload(parts), max_attempts=max_attempts
        )
        return self.extract_text(resp.json()).strip()


    def requires_api_key(self) -> bool:
        """Whether a scan can even be attempted without a credential."""
        return True

class GeminiVisionProvider(VisionProvider):
    name = "gemini"
    settings_key = "gemini_api_key"
    env_key = "GEMINI_API_KEY"
    label = "Gemini"

    def model(self) -> str:
        return get_gemini_model()

    def build_payload(self, parts: list[dict]) -> dict:
        gemini_parts = []
        for part in parts:
            if part["type"] == "text":
                gemini_parts.append({"text": part["text"]})
            else:
                gemini_parts.append({
                    "inline_data": {
                        "mime_type": part["mime_type"],
                        "data": part["data"],
                    }
                })
        return {"contents": [{"parts": gemini_parts}]}

    def extract_text(self, data: dict) -> str:
        return data["candidates"][0]["content"]["parts"][0]["text"]

    async def post(self, client, api_key: str, payload: dict, *, max_attempts: int = 3):
        return await post_gemini_generate(
            client, build_gemini_generate_url(), api_key, payload, max_attempts=max_attempts
        )


class OpenAIVisionProvider(VisionProvider):
    name = "openai"
    settings_key = "openai_api_key"
    env_key = "OPENAI_API_KEY"
    label = "OpenAI"

    def model(self) -> str:
        return get_openai_model()

    def requires_api_key(self) -> bool:
        # Only the hosted API needs a credential. Pointing OPENAI_BASE_URL at
        # Ollama, llama.cpp or LM Studio means there is usually no key to give.
        return openai_base_url() == DEFAULT_OPENAI_BASE_URL

    def build_payload(self, parts: list[dict]) -> dict:
        content = []
        for part in parts:
            if part["type"] == "text":
                content.append({"type": "text", "text": part["text"]})
            else:
                data_uri = f"data:{part['mime_type']};base64,{part['data']}"
                content.append({"type": "image_url", "image_url": {"url": data_uri}})
        # No max_tokens/max_completion_tokens: the two are mutually exclusive
        # across model generations and the replies here are a few dozen tokens,
        # so the API default is both safe and portable.
        return {
            "model": self.model(),
            "messages": [{"role": "user", "content": content}],
        }

    def extract_text(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"]

    async def post(self, client, api_key: str, payload: dict, *, max_attempts: int = 3):
        return await post_openai_chat(client, api_key, payload, max_attempts=max_attempts)


PROVIDERS = {
    GeminiVisionProvider.name: GeminiVisionProvider,
    OpenAIVisionProvider.name: OpenAIVisionProvider,
}


def resolve_provider(name: str | None = None) -> VisionProvider:
    """Return the configured provider, falling back to Gemini for unknown names."""
    requested = (name or "").strip().lower()
    if not requested:
        requested = os.environ.get("SCANNER_PROVIDER", "").strip().lower()
    if not requested:
        requested = DEFAULT_PROVIDER
    provider_cls = PROVIDERS.get(requested, PROVIDERS[DEFAULT_PROVIDER])
    return provider_cls()
