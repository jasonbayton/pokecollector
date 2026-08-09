import base64
import httpx
import os
import json
import re
from services.tcgdex_languages import is_supported_tcgdex_language, normalize_tcgdex_language
import logging
from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from sqlalchemy.orm import Session
from api.auth import get_current_user
from database import get_db, get_setting
from models import Setting, UserSetting, User, Set
from services.card_numbers import card_number_variants
from services.vision_provider import (  # noqa: F401  (re-exported for callers/tests)
    DEFAULT_GEMINI_MODEL,
    DEFAULT_OPENAI_MODEL,
    TRANSIENT_STATUS_CODES as GEMINI_TRANSIENT_STATUS_CODES,
    build_gemini_generate_url,
    gemini_error_message,
    get_gemini_model,
    get_openai_model,
    image_part,
    post_gemini_generate,
    post_openai_chat,
    resolve_provider,
    shared_scanner_key_allowed,
    text_part,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SCANNER_PROVIDER_SETTING = "scanner_provider"


def normalize_scanner_card_number(value) -> str | None:
    """Return the leading collector number without zeros, if one is present."""
    if value is None:
        return None
    match = re.match(r"(\d+)", str(value).strip())
    return str(int(match.group(1))) if match else None


def _number_match_keys(value) -> set:
    """Comparable forms of a printed number, casefolded.

    Both sides of a comparison go through this, so "63/100" and "063" meet on
    "63" while the set total is ignored.
    """
    return {variant.casefold() for variant in card_number_variants(value)}


def prioritize_cards_by_number(
    cards: list[dict],
    recognized_number,
    *,
    number_field: str = "number",
) -> tuple[list[dict], int]:
    """Stable-partition cards so recognized collector-number matches come first.

    Matching is on shared number variants rather than the leading digits, for
    two reasons. Trainer gallery and promo numbers such as "TG01" have no
    leading digits at all, so a digits-only rule cannot prioritise them and the
    card stays buried below the candidate cap - the exact truncation this
    function exists to prevent. And "74a" reduced to "74" names a different,
    real card, so a suffixed number must not match the plain one.
    """
    wanted = _number_match_keys(recognized_number)
    if not wanted:
        return cards, 0

    matches = []
    rest = []
    for card in cards:
        candidate = _number_match_keys(card.get(number_field))
        (matches if candidate & wanted else rest).append(card)

    if not matches:
        return cards, 0
    return matches + rest, len(matches)


def get_scanner_provider():
    """Resolve the configured vision provider.

    Order of precedence: the admin-set ``scanner_provider`` setting, then the
    SCANNER_PROVIDER env var, then Gemini.
    """
    return resolve_provider(get_setting(SCANNER_PROVIDER_SETTING))


def get_provider_key(db: Session, provider, user_id: int = None) -> str:
    """Read the provider's API key: the user's own first, then the environment.

    The environment is consulted only when ALLOW_SHARED_SCANNER_KEY is set, so
    the default remains one key per user.
    """
    if user_id is not None:
        row = db.query(UserSetting).filter(
            UserSetting.user_id == user_id, UserSetting.key == provider.settings_key
        ).first()
        # Whitespace is not a key: a blank override must not shadow the
        # environment, nor be sent upstream as credentials.
        if row and row.value and row.value.strip():
            return row.value.strip()
    if not shared_scanner_key_allowed():
        return ""
    return os.environ.get(provider.env_key, "").strip()


def get_gemini_key(db: Session, user_id: int = None) -> str:
    """Backwards-compatible accessor for the Gemini key."""
    from services.vision_provider import GeminiVisionProvider

    return get_provider_key(db, GeminiVisionProvider(), user_id=user_id)


@router.post("/recognize")
async def recognize_card(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Accepts a card image, uses the configured vision provider to extract card
    details including the card's language, then searches TCGdex in that language.
    Supports configured TCGdex card languages automatically.
    """
    provider = get_scanner_provider()
    api_key = get_provider_key(db, provider, user_id=current_user.id)
    if not api_key and provider.requires_api_key():
        raise HTTPException(
            status_code=400,
            detail=f"Kein {provider.label} API Key konfiguriert. Bitte in den Einstellungen eintragen."
        )

    # Read image
    image_bytes = await file.read()
    image_b64 = base64.b64encode(image_bytes).decode()
    mime_type = file.content_type or "image/jpeg"

    prompt = """Look at this Pokemon Trading Card Game card image. Extract the following:
1. Card name (exactly as printed on the card, in the card's language)
2. Card name in English (if the card is not English, give the English name; if already English, same as above)
3. Card number (e.g. "136/182" — printed at the bottom)
4. Set name or abbreviation if visible
5. Card type (Pokemon, Trainer, or Energy)
6. HP value if it's a Pokemon card
7. Language of the card (2-letter ISO code: "en" for English, "de" for German, "fr" for French, "es" for Spanish, "it" for Italian, "pt" for Portuguese, "ja" for Japanese, etc.)

Respond ONLY with this exact JSON (no markdown, no explanation):
{
  "name": "card name in card's language",
  "name_en": "card name in English (same as name if card is English)",
  "number": "card number or null",
  "set_hint": "set name or abbreviation or null",
  "card_type": "Pokemon/Trainer/Energy",
  "hp": "HP value or null",
  "language": "en"
}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            text = await provider.generate(client, api_key, [
                text_part(prompt),
                image_part(mime_type, image_b64),
            ])

        # Parse JSON from the model response (handles markdown code blocks too)
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if not json_match:
            raise ValueError(f"No JSON found in {provider.label} response")
        card_info = json.loads(json_match.group())

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erkennung fehlgeschlagen: {str(e)}")

    card_name = card_info.get("name", "").strip()
    card_name_en = card_info.get("name_en", card_name).strip() or card_name
    if not card_name:
        raise HTTPException(status_code=422, detail="Kartenname konnte nicht erkannt werden.")

    # Strip card suffixes for broader TCGdex search — exact variants differ between
    # printed text ("EX") and TCGdex naming ("ex", "-ex"). The number ranking and
    # visual verification will find the exact match from the broader result set.
    _SUFFIXES = re.compile(
        r"[\s-]+(?:EX|ex|GX|gx|V|VMAX|VSTAR|VStar|TAG\s*TEAM|BREAK|LV\.?\s*X)\s*$",
        re.IGNORECASE,
    )

    def _simplify_name(name: str) -> str:
        return _SUFFIXES.sub("", name).strip()

    card_name_simple = _simplify_name(card_name)
    card_name_en_simple = _simplify_name(card_name_en)

    # Use detected language for TCGdex search.
    detected_lang = normalize_tcgdex_language(card_info.get("language", "en"))
    if not is_supported_tcgdex_language(detected_lang):
        detected_lang = "en"

    # Build (lang, search_name) pairs — try simplified name first (broader), then original as fallback
    search_pairs = [(detected_lang, card_name_simple)]
    if card_name_simple != card_name:
        search_pairs.append((detected_lang, card_name))
    if detected_lang != "en":
        search_pairs.append(("en", card_name_en_simple))
        if card_name_en_simple != card_name_en:
            search_pairs.append(("en", card_name_en))

    # TCGdex returns search results sorted ascending by card number, so a plain
    # head slice keeps only the lowest-numbered printings and discards the
    # target card for anything numbered above them. Float printings that match
    # the recognized number to the front so they survive the per-search cap.
    recognized_number = card_info.get("number")

    # Collect all raw results first, setting _lang on each card
    all_results = []
    for lang, search_name in search_pairs:
        if len(all_results) >= 15:
            break
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                search_resp = await client.get(
                    f"https://api.tcgdex.net/v2/{lang}/cards",
                    params={"name": search_name}
                )
            if search_resp.status_code == 200:
                tcgdex_cards = search_resp.json()
                if isinstance(tcgdex_cards, list):
                    logger.info(f"TCGdex {lang} search for '{search_name}': {len(tcgdex_cards)} results")
                    prioritized_cards, match_count = prioritize_cards_by_number(
                        tcgdex_cards,
                        recognized_number,
                        number_field="localId",
                    )
                    if match_count:
                        logger.info(
                            "Number pre-filter: %s of %s results match #%s",
                            match_count,
                            len(tcgdex_cards),
                            normalize_scanner_card_number(recognized_number),
                        )
                    for c in prioritized_cards[:8]:
                        card_id = c.get("id")
                        if not card_id:
                            continue
                        composite_id = f"{card_id}_{lang}"
                        all_results.append({
                            "id": composite_id,
                            "tcg_card_id": card_id,
                            "name": c.get("name"),
                            "set": c.get("set", {}).get("name") if isinstance(c.get("set"), dict) else None,
                            "number": c.get("localId"),
                            "image": f"{c.get('image')}/low.webp" if c.get("image") else None,
                            "rarity": c.get("rarity"),
                            "lang": lang,
                            "_lang": lang,  # internal dedup key field
                        })
        except Exception:
            continue

    # Enrich results with set name from local DB
    for card in all_results:
        tcg_card_id = card.get("tcg_card_id", "")
        card_lang = card.get("_lang", "en")
        # Extract set_id from card_id: "me02.5-022" -> "me02.5"
        if "-" in tcg_card_id:
            set_id = tcg_card_id.rsplit("-", 1)[0]
            local_set = db.query(Set).filter(
                Set.tcg_set_id == set_id, Set.lang == card_lang
            ).first()
            if not local_set:
                # Fallback: try without language filter
                local_set = db.query(Set).filter(Set.tcg_set_id == set_id).first()
            if local_set:
                card["set"] = local_set.name
                if local_set.abbreviation:
                    card["set_abbreviation"] = local_set.abbreviation

    # Dedup by (card_id, _lang) composite key — same card in different languages counts once per lang
    seen = set()
    deduped = []
    for card in all_results:
        key = (card.get('id'), card.get('_lang', 'en'))
        if key not in seen:
            seen.add(key)
            deduped.append(card)

    logger.info(
        f"Recognize dedup: {len(all_results)} before -> {len(deduped)} after dedup by (card_id, _lang)"
    )

    # Rank results: cards with matching number first
    deduped, number_match_count = prioritize_cards_by_number(
        deduped,
        recognized_number,
    )
    number_match_clear = number_match_count == 1
    if number_match_count:
        logger.info(
            "Ranked results by number match (target: %s)",
            normalize_scanner_card_number(recognized_number),
        )

    # Visual verification: ask the provider to pick the best match from candidate
    # images. Skip this second call when number ranking is decisive or there
    # are not enough candidate images to compare visually.
    top_candidates = [card for card in deduped[:5] if card.get("image")]  # max 5 to keep costs low
    if len(top_candidates) >= 2 and not number_match_clear:
        try:
            # Download candidate images
            candidate_parts = [
                text_part("Here is the original card photo the user took:"),
                image_part(mime_type, image_b64),
                text_part(
                    "Below are candidate cards from our database. Which one matches the photo "
                    "above? Look at the artwork, card name, and card number. Respond with ONLY "
                    "the number (1, 2, 3...) of the best match, or 0 if none match.\n"
                ),
            ]

            async with httpx.AsyncClient(timeout=20) as client:
                for i, candidate in enumerate(top_candidates):
                    img_url = candidate.get("image")
                    if not img_url:
                        candidate_parts.append(text_part(
                            f"\nCandidate {i + 1}: {candidate.get('name', '?')} (no image available)"
                        ))
                        continue
                    try:
                        img_resp = await client.get(img_url, timeout=5)
                        if img_resp.status_code == 200:
                            img_b64 = base64.b64encode(img_resp.content).decode()
                            candidate_parts.append(text_part(
                                f"\nCandidate {i + 1}: {candidate.get('name', '?')} "
                                f"#{candidate.get('number', '?')}"
                            ))
                            candidate_parts.append(image_part("image/webp", img_b64))
                        else:
                            candidate_parts.append(text_part(
                                f"\nCandidate {i + 1}: {candidate.get('name', '?')} "
                                "(image unavailable)"
                            ))
                    except Exception:
                        candidate_parts.append(text_part(
                            f"\nCandidate {i + 1}: {candidate.get('name', '?')} "
                            "(image fetch failed)"
                        ))

                verify_text = await provider.generate(
                    client, api_key, candidate_parts, max_attempts=2
                )

            if verify_text:
                # Extract the number from response
                pick_match = re.search(r"(\d+)", verify_text)
                if pick_match:
                    pick = int(pick_match.group(1))
                    if 1 <= pick <= len(top_candidates):
                        # Move the picked candidate to the front
                        winner = top_candidates[pick - 1]
                        deduped.remove(winner)
                        deduped.insert(0, winner)
                        logger.info(
                            f"Visual verification picked candidate {pick}: "
                            f"{winner.get('name')} #{winner.get('number')}"
                        )
                    elif pick == 0:
                        logger.info("Visual verification: no match found among candidates")
        except Exception as e:
            logger.warning(f"Visual verification failed (non-blocking): {e}")

    return {
        "recognized": card_info,
        "matches": deduped[:8],
    }
