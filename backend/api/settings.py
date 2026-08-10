import logging
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from api.auth import get_current_user
from sqlalchemy.orm import Session
from database import get_db
from models import Setting, UserSetting, User
from services.debug_logging import configure_debug_logging, get_debug_log_path
from services.digital_sets import DIGITAL_SETS_SETTING_KEY, refresh_digital_catalogue_flags
from services.exchange_rates import (
    ExchangeRateError,
    fallback_exchange_rate,
    normalize_currency_pair,
    parse_frankfurter_v2_rate,
)
from services.card_visibility import get_visible_filter_languages
from services.exchange_rates import SUPPORTED_CURRENCIES
from services.public_profile_feature import PUBLIC_PROFILES_SETTING_KEY
from services.tcgdex_languages import (
    DEFAULT_TCGDEX_SYNC_LANGUAGES,
    supported_tcgdex_language_payload,
    validate_tcgdex_sync_languages,
)
from services.scan_trace import (
    SCAN_DIAGNOSTICS_SETTING_KEY,
    delete_user_traces,
    trace_available,
    trace_deletion_available,
)

from services.scan_providers import (
    PROVIDERS,
    SCANNER_MODEL_SETTING,
    ScanProvider,
    resolve_provider_name,
    visual_verification_default,
    SCANNER_PROVIDER_SETTING,
    VISUAL_VERIFICATION_SETTING,
)

router = APIRouter()
logger = logging.getLogger(__name__)

PER_USER_KEYS = {
    "language", "currency", "price_primary", "price_display",
    "set_overview_filters", "hidden_set_ids",
    "telegram_bot_token", "telegram_chat_id", "telegram_enabled",
    "price_alerts_enabled", "price_alert_threshold",
    "gemini_api_key", "trainer_name", "portfolio_display_mode",
    "openai_api_key", "share_collection",
    SCANNER_PROVIDER_SETTING, VISUAL_VERIFICATION_SETTING, SCANNER_MODEL_SETTING,
    SCAN_DIAGNOSTICS_SETTING_KEY,
}

ADMIN_ONLY_KEYS = {
    "full_sync_interval_days", "price_sync_interval_minutes", "multi_user_mode",
    "tcgdex_sync_languages", "debug_mode",
    "cross_language_price_fallback", "cross_language_image_fallback",
    DIGITAL_SETS_SETTING_KEY,
    PUBLIC_PROFILES_SETTING_KEY,
}

# Settings that can be supplied by the service environment when the user has
# not set one of their own. The UI uses this to show where a value came from
# and to offer an override.
ENV_BACKED_KEYS = {
    "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
    "telegram_chat_id": "TELEGRAM_CHAT_ID",
    "gemini_api_key": "GEMINI_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "scanner_provider": "SCANNER_PROVIDER",
}

# Credentials. Never returned to the browser in full, whoever set them; the UI
# only needs to know one is present.
SECRET_KEYS = {"telegram_bot_token", "gemini_api_key", "openai_api_key"}

# Scanner credentials, which only fall back to the environment when the
# operator has opted into sharing one key across accounts.
SCANNER_KEY_SETTINGS = {"gemini_api_key", "openai_api_key"}


def _default_currency() -> str:
    """Installation default currency, validated so a typo cannot leave every
    new account on an unsupported code the frontend would render as euro."""
    configured = os.environ.get("DEFAULT_CURRENCY", "").strip().upper()
    if not configured:
        return "EUR"
    if configured not in SUPPORTED_CURRENCIES:
        logger.warning(
            "DEFAULT_CURRENCY=%r is not supported (%s); falling back to EUR",
            configured, ", ".join(sorted(SUPPORTED_CURRENCIES)),
        )
        return "EUR"
    return configured

DEFAULT_SETTINGS = {
    "trainer_name": "TRAINER",
    "full_sync_interval_days": "5",
    "price_sync_interval_minutes": "30",
    "telegram_enabled": "false",
    "telegram_chat_id": "",
    "price_alerts_enabled": "false",
    "price_alert_threshold": "10",
    "language": "en",
    # Env-driven so an installation can pick its own default without a code
    # change, and so new accounts inherit it rather than starting on EUR.
    "currency": _default_currency(),
    "price_primary": "trend",
    "portfolio_display_mode": "portfolio_value",
    "price_display": '["trend", "avg", "avg1", "avg7", "avg30", "low"]',
    "set_overview_filters": "{}",
    "hidden_set_ids": "[]",
    "tcgdex_sync_languages": "en,de",
    DIGITAL_SETS_SETTING_KEY: "true",
    "cross_language_price_fallback": "true",
    "cross_language_image_fallback": "true",
    "debug_mode": "false",
    "scanner_provider": "gemini",
    # Opt-in, so nobody is contributed to the shared server view by default.
    "share_collection": "false",
    PUBLIC_PROFILES_SETTING_KEY: "false",
    SCAN_DIAGNOSTICS_SETTING_KEY: "false",
    # Existing installations have no stored provider, and this keeps them on
    # Gemini exactly as before.
    SCANNER_PROVIDER_SETTING: "gemini",
}


def _normalize_tcgdex_sync_languages(value) -> str:
    try:
        return validate_tcgdex_sync_languages(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _coerce_setting_value(key: str, value) -> str:
    if key == "tcgdex_sync_languages":
        return _normalize_tcgdex_sync_languages(value)
    if key in {
        "debug_mode", "cross_language_price_fallback",
        "cross_language_image_fallback", DIGITAL_SETS_SETTING_KEY,
        PUBLIC_PROFILES_SETTING_KEY, SCAN_DIAGNOSTICS_SETTING_KEY,
        "share_collection",
    }:
        return "true" if str(value).lower() in {"true", "1", "yes", "on"} else "false"
    if key == SCANNER_PROVIDER_SETTING:
        # Rejected at write time rather than falling back silently at scan time,
        # so a typo surfaces here instead of quietly scanning with the wrong one.
        provider = str(value).strip().lower()
        if provider not in PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported scanner provider. Choose one of: {', '.join(sorted(PROVIDERS))}.",
            )
        return provider
    if key == SCANNER_MODEL_SETTING:
        # Free text, because provider model names change constantly. Trimmed so a
        # value of spaces means "use the installation default" rather than being
        # sent upstream as a model name, and bounded so it cannot be used to
        # stuff arbitrary content into the request.
        model = str(value).strip()
        if len(model) > 100:
            raise HTTPException(status_code=422, detail="Model name is too long.")
        return model
    if key == VISUAL_VERIFICATION_SETTING:
        return "true" if str(value).strip().lower() in {"true", "1", "yes", "on"} else "false"
    if key == "portfolio_display_mode":
        normalized = str(value).strip().lower()
        if normalized not in {"portfolio_value", "capital_invested"}:
            raise HTTPException(status_code=422, detail="portfolio_display_mode is invalid")
        return normalized
    if key == "scanner_provider":
        # Reject unknown providers rather than letting resolve_provider() quietly
        # fall back to the default, which hides a typo until a scan fails.
        provider = str(value).strip().lower()
        if provider not in PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported scanner provider. Choose one of: {', '.join(sorted(PROVIDERS))}.",
            )
        return provider
    if key == "currency":
        currency = str(value).strip().upper()
        if currency not in SUPPORTED_CURRENCIES:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported currency. Choose one of: {', '.join(sorted(SUPPORTED_CURRENCIES))}.",
            )
        return currency
    if key in SECRET_KEYS:
        # Stored untrimmed, a key of spaces looks configured and shadows the
        # environment while failing every request it is used for.
        return str(value).strip()
    return str(value)


def _apply_setting_side_effect(db: Session, key: str, value: str) -> None:
    if key == "debug_mode":
        enabled = value == "true"
        configure_debug_logging(enabled)
        logger.info("Debug mode setting changed to %s", enabled)
    elif key == DIGITAL_SETS_SETTING_KEY:
        result = refresh_digital_catalogue_flags(db)
        logger.info(
            "Digital set visibility changed to %s; marked %s digital sets and %s digital cards",
            value == "true",
            result["sets_marked"],
            result["cards_marked"],
        )


def _is_admin(db: Session, user_id: int) -> bool:
    user = db.query(User).filter(User.id == user_id).first()
    return user is not None and user.role == "admin"


def _get_user_settings(db: Session, user_id: int) -> dict:
    """Get all settings for a user: per-user from user_settings, global from settings."""
    result = {}

    # Only load admin-only keys from global settings
    for row in db.query(Setting).all():
        if row.key in ADMIN_ONLY_KEYS:
            result[row.key] = row.value

    # Load this user's own settings
    for row in db.query(UserSetting).filter(UserSetting.user_id == user_id).all():
        result[row.key] = row.value

    # A cleared override leaves an empty user row behind. Treat empty as absent,
    # otherwise it shadows the environment value here while key resolution still
    # falls through to it, and the two disagree about what is actually in use.
    is_admin = _is_admin(db, user_id)
    for key, env_name in ENV_BACKED_KEYS.items():
        if result.get(key):
            continue
        # Scanner keys fall back to the installation key for every account, so
        # every account is told one is configured. Other environment-backed
        # settings stay admin-only, as before.
        # Scanner keys and the scanner provider are installation-wide fallbacks
        # that apply to every account, so every account is shown what it will
        # actually use. Other environment-backed settings stay admin-only.
        if key not in SCANNER_KEY_SETTINGS and key != SCANNER_PROVIDER_SETTING and not is_admin:
            continue
        env_value = os.environ.get(env_name, "").strip()
        if env_value:
            result[key] = env_value.lower() if key == "scanner_provider" else env_value

    for key, value in DEFAULT_SETTINGS.items():
        result.setdefault(key, value)
    result["scan_diagnostics_available"] = "true" if trace_available() else "false"
    # Published so the UI shows the state the scanner will actually use. The
    # rule depends on OPENAI_BASE_URL, which the browser cannot see.
    # A local endpoint needs no credential, so the UI must not warn that scanning
    # will fail when it will not.
    result["scanner_key_required"] = (
        "true" if ScanProvider(resolve_provider_name(db, user_id)).requires_credential() else "false"
    )
    # Published so the field can show what a blank value will actually use.
    result["scanner_model_default"] = ScanProvider(
        resolve_provider_name(db, user_id)
    ).installation_model()
    result["scanner_visual_verification_default"] = (
        "true" if visual_verification_default(resolve_provider_name(db, user_id)) else "false"
    )
    result["scan_diagnostics_deletion_available"] = (
        "true" if trace_deletion_available() else "false"
    )

    return result


def _redact_secrets(settings: dict) -> dict:
    """Blank every credential before a settings dict reaches the browser.

    Redacting only environment-sourced values was not enough. A global
    GEMINI_API_KEY is copied into the admin's user_settings by the v42 startup
    migration, after which it looks user-set and skipped the mask. Nothing needs
    the plaintext value client-side, so none of them are returned: the per-key
    endpoint reports `configured` and a masked `hint` instead.
    """
    redacted = dict(settings)
    for key in SECRET_KEYS:
        if redacted.get(key):
            redacted[key] = ""
    return redacted


@router.get("/")
def get_settings(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _redact_secrets(_get_user_settings(db, current_user.id))


@router.get("/tcgdex-languages")
def get_tcgdex_languages(current_user: User = Depends(get_current_user)):
    return {
        "languages": supported_tcgdex_language_payload(),
        "default": list(DEFAULT_TCGDEX_SYNC_LANGUAGES),
        "english_fallback": "en",
    }


@router.get("/tcgdex-filter-languages")
def get_tcgdex_filter_languages(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    visible_codes = set(get_visible_filter_languages(db, current_user.id))
    return {
        "languages": [
            language for language in supported_tcgdex_language_payload()
            if language["code"] in visible_codes
        ],
    }


@router.put("/")
def update_settings(data: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    pending_side_effects = []
    for key, value in data.items():
        coerced_value = _coerce_setting_value(key, value)
        if key in ADMIN_ONLY_KEYS:
            if current_user.role != "admin":
                if key == PUBLIC_PROFILES_SETTING_KEY:
                    raise HTTPException(status_code=403, detail="Admin only")
                continue
            row = db.query(Setting).filter(Setting.key == key).first()
            if row:
                row.value = coerced_value
            else:
                db.add(Setting(key=key, value=coerced_value))
            pending_side_effects.append((key, coerced_value))
        else:
            row = db.query(UserSetting).filter(
                UserSetting.user_id == current_user.id, UserSetting.key == key
            ).first()
            if row:
                row.value = coerced_value
            else:
                db.add(UserSetting(user_id=current_user.id, key=key, value=coerced_value))
    for key, value in pending_side_effects:
        _apply_setting_side_effect(db, key, value)
    db.commit()
    return _redact_secrets(_get_user_settings(db, current_user.id))


@router.get("/debug-log")
def download_debug_log(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    path = get_debug_log_path()
    return Response(
        content=path.read_bytes(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="pokecollector-debug.log"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.delete("/scan-diagnostics")
def delete_scan_diagnostics(
    current_user: User = Depends(get_current_user),
):
    try:
        deleted = delete_user_traces(current_user.id)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail="Stored scanner diagnostics could not be deleted.",
        ) from exc
    return {"deleted": deleted}


@router.get("/telegram_status")
def get_telegram_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    settings = _get_user_settings(db, current_user.id)
    token = settings.get("telegram_bot_token", "")
    chat_id = settings.get("telegram_chat_id", "")
    return {"configured": bool(token and chat_id)}


@router.get("/exchange-rate")
def get_exchange_rate(
    from_currency: str = Query(alias="from"),
    to_currency: str = Query(alias="to"),
    _current_user: User = Depends(get_current_user),
):
    try:
        source, target = normalize_currency_pair(from_currency, to_currency)
    except ExchangeRateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    fallback_rate = fallback_exchange_rate(source, target)
    if source == target:
        return {"from": source, "to": target, "rate": fallback_rate, "fallback": False}

    try:
        response = httpx.get(
            f"https://api.frankfurter.dev/v2/rate/{source}/{target}",
            timeout=8,
        )
        response.raise_for_status()
        rate = parse_frankfurter_v2_rate(response.json())
        return {"from": source, "to": target, "rate": rate, "fallback": False}
    except Exception as exc:
        logger.warning("Failed to fetch exchange rate %s to %s: %s", source, target, exc)
        return {"from": source, "to": target, "rate": fallback_rate, "fallback": True}


def _mask_secret(value: str) -> str:
    """Enough of a secret to recognise it, not enough to use it."""
    v = (value or "").strip()
    if len(v) <= 4:
        return "•" * 4
    return "•" * 4 + v[-4:]


def _setting_source(db: Session, user_id: int, key: str) -> str:
    """Where a setting's effective value came from: user, env, or default."""
    if key in ADMIN_ONLY_KEYS:
        row = db.query(Setting).filter(Setting.key == key).first()
    else:
        row = db.query(UserSetting).filter(
            UserSetting.user_id == user_id, UserSetting.key == key
        ).first()
    if row and row.value:
        return "user"
    env_name = ENV_BACKED_KEYS.get(key)
    if env_name and os.environ.get(env_name, "").strip():
        # A scanner key set on the installation is the fallback for every
        # account, so report it as configured for non-admins too. Telling a user
        # their key is unset while their scans quietly succeed on the shared one
        # is worse than saying nothing.
        if key in SCANNER_KEY_SETTINGS or _is_admin(db, user_id):
            return "env"
    return "default"


@router.get("/{key}")
def get_setting(key: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if key == "sync_interval_hours":
        settings = _get_user_settings(db, current_user.id)
        days = int(settings.get("full_sync_interval_days", "5"))
        return {
            "key": key,
            "value": str(days * 24),
            "source": _setting_source(db, current_user.id, "full_sync_interval_days"),
        }
    settings = _get_user_settings(db, current_user.id)
    if key in settings:
        source = _setting_source(db, current_user.id, key)
        value = settings[key]
        payload = {"key": key, "value": value, "source": source}
        if key in SECRET_KEYS:
            # No credential goes back to the browser, whoever set it. The UI
            # only needs to know one exists and roughly which one it is.
            payload["value"] = ""
            payload["configured"] = bool(value)
            if value:
                payload["hint"] = _mask_secret(value)
        return payload
    raise HTTPException(status_code=404, detail=f"Setting {key} not found")


@router.post("/{key}")
def set_setting(key: str, body: dict, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    value = _coerce_setting_value(key, body.get("value", ""))
    if key in ADMIN_ONLY_KEYS:
        if current_user.role != "admin":
            raise HTTPException(status_code=403, detail="Admin only")
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        pending_side_effect = (key, value)
    else:
        row = db.query(UserSetting).filter(
            UserSetting.user_id == current_user.id, UserSetting.key == key
        ).first()
        if row:
            row.value = value
        else:
            db.add(UserSetting(user_id=current_user.id, key=key, value=value))
    if key in ADMIN_ONLY_KEYS:
        _apply_setting_side_effect(db, *pending_side_effect)
    db.commit()
    payload = {"key": key, "value": value, "source": _setting_source(db, current_user.id, key)}
    if key in SECRET_KEYS:
        payload["value"] = ""
        payload["configured"] = bool(value)
        if value:
            payload["hint"] = _mask_secret(value)
    return payload
