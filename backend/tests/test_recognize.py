import unittest
from unittest.mock import patch

try:
    import httpx
    from fastapi import HTTPException

    from api.recognize import (
        DEFAULT_GEMINI_MODEL,
        build_gemini_generate_url,
        get_gemini_model,
        gemini_error_message,
        normalize_scanner_card_number,
        post_gemini_generate,
        prioritize_cards_by_number,
    )
    from services.vision_provider import (
        DEFAULT_OPENAI_MODEL,
        GeminiVisionProvider,
        OpenAIVisionProvider,
        get_openai_model,
        image_part,
        post_openai_chat,
        resolve_provider,
        text_part,
    )
    API_TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    API_TEST_DEPS_AVAILABLE = False


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class RecognizeConfigTests(unittest.TestCase):
    def test_gemini_model_defaults_to_supported_alias(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_gemini_model(), DEFAULT_GEMINI_MODEL)
            self.assertIn(f"/{DEFAULT_GEMINI_MODEL}:generateContent", build_gemini_generate_url())

    def test_gemini_model_uses_env_and_accepts_models_prefix(self):
        with patch.dict("os.environ", {"GEMINI_MODEL": "models/gemini-3.5-flash"}):
            self.assertEqual(get_gemini_model(), "gemini-3.5-flash")
            self.assertIn("/gemini-3.5-flash:generateContent", build_gemini_generate_url())

    def test_openai_model_defaults_and_honours_env(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(get_openai_model(), DEFAULT_OPENAI_MODEL)
        with patch.dict("os.environ", {"OPENAI_MODEL": "gpt-4.1-mini"}):
            self.assertEqual(get_openai_model(), "gpt-4.1-mini")


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class ProviderResolutionTests(unittest.TestCase):
    def test_defaults_to_gemini(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(resolve_provider(None), GeminiVisionProvider)

    def test_explicit_name_wins(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(resolve_provider("openai"), OpenAIVisionProvider)
            self.assertIsInstance(resolve_provider("OpenAI"), OpenAIVisionProvider)

    def test_env_used_when_no_explicit_name(self):
        with patch.dict("os.environ", {"SCANNER_PROVIDER": "openai"}):
            self.assertIsInstance(resolve_provider(None), OpenAIVisionProvider)

    def test_unknown_provider_falls_back_to_gemini(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsInstance(resolve_provider("llama-vision"), GeminiVisionProvider)


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class PayloadShapeTests(unittest.TestCase):
    # Built per-test, not in the class body: the body is evaluated at import
    # time even when the skip decorator applies, so referencing text_part there
    # raises NameError and breaks discovery in the dependency-free test mode.
    @property
    def PARTS(self):
        return [text_part("describe this"), image_part("image/jpeg", "AAAA")]

    def test_gemini_payload_uses_inline_data(self):
        payload = GeminiVisionProvider().build_payload(self.PARTS)
        parts = payload["contents"][0]["parts"]
        self.assertEqual(parts[0], {"text": "describe this"})
        self.assertEqual(
            parts[1], {"inline_data": {"mime_type": "image/jpeg", "data": "AAAA"}}
        )

    def test_openai_payload_uses_data_uri_image_url(self):
        with patch.dict("os.environ", {}, clear=True):
            payload = OpenAIVisionProvider().build_payload(self.PARTS)
        self.assertEqual(payload["model"], DEFAULT_OPENAI_MODEL)
        content = payload["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "describe this"})
        self.assertEqual(
            content[1],
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
        )

    def test_openai_payload_omits_token_limit(self):
        # max_tokens and max_completion_tokens are mutually exclusive across
        # model generations; sending neither is the portable choice.
        with patch.dict("os.environ", {}, clear=True):
            payload = OpenAIVisionProvider().build_payload(self.PARTS)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("max_completion_tokens", payload)

    def test_providers_extract_their_own_response_text(self):
        self.assertEqual(
            GeminiVisionProvider().extract_text(
                {"candidates": [{"content": {"parts": [{"text": "  hi  "}]}}]}
            ).strip(),
            "hi",
        )
        self.assertEqual(
            OpenAIVisionProvider().extract_text(
                {"choices": [{"message": {"content": "  hi  "}}]}
            ).strip(),
            "hi",
        )


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class RecognizeCardNumberTests(unittest.TestCase):
    def test_normalizes_leading_zeros_and_fractional_printed_numbers(self):
        self.assertEqual(normalize_scanner_card_number("063"), "63")
        self.assertEqual(normalize_scanner_card_number("136/182"), "136")

    def test_rejects_missing_and_non_leading_numbers(self):
        self.assertIsNone(normalize_scanner_card_number(None))
        self.assertIsNone(normalize_scanner_card_number(""))
        self.assertIsNone(normalize_scanner_card_number("No. 039"))
        self.assertIsNone(normalize_scanner_card_number("TG01"))

    def test_high_numbered_match_survives_candidate_cap(self):
        cards = [
            {"id": f"card-{number}", "localId": str(number)}
            for number in range(1, 65)
        ]

        prioritized, match_count = prioritize_cards_by_number(
            cards,
            "63/100",
            number_field="localId",
        )

        self.assertEqual(match_count, 1)
        self.assertEqual(prioritized[0]["id"], "card-63")
        self.assertIn("card-63", [card["id"] for card in prioritized[:8]])

    def test_leading_zero_matches_and_preserves_stable_order(self):
        cards = [
            {"id": "before", "number": "5"},
            {"id": "first-match", "number": "063"},
            {"id": "between", "number": "9"},
            {"id": "second-match", "number": "63/100"},
            {"id": "after", "number": "70"},
        ]

        prioritized, match_count = prioritize_cards_by_number(cards, "063/100")

        self.assertEqual(match_count, 2)
        self.assertEqual(
            [card["id"] for card in prioritized],
            ["first-match", "second-match", "before", "between", "after"],
        )

    def test_missing_unusual_or_unmatched_number_keeps_original_order(self):
        cards = [
            {"id": "first", "number": "1"},
            {"id": "second", "number": "2"},
        ]

        for recognized_number in (None, "No. 039", "999"):
            with self.subTest(recognized_number=recognized_number):
                prioritized, match_count = prioritize_cards_by_number(
                    cards,
                    recognized_number,
                )
                self.assertIs(prioritized, cards)
                self.assertEqual(match_count, 0)


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class RecognizeErrorTests(unittest.TestCase):
    def test_extracts_gemini_error_message(self):
        response = httpx.Response(404, json={"error": {"message": "model retired"}})

        self.assertEqual(gemini_error_message(response), "model retired")


class _StatusClient:
    """Async client stub that always answers with one canned response."""

    def __init__(self, response):
        self._response = response
        self.calls = []

    async def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        return self._response


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class RecognizeApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_404_surfaces_upstream_message(self):
        class FakeClient:
            async def post(self, *args, **kwargs):
                return httpx.Response(
                    404,
                    json={"error": {"message": "This model is no longer available to new users."}},
                )

        with self.assertRaises(HTTPException) as ctx:
            await post_gemini_generate(FakeClient(), "https://example.test", "key", {})

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("GEMINI_MODEL", ctx.exception.detail)
        self.assertIn("no longer available", ctx.exception.detail)

    async def test_openai_404_surfaces_upstream_message_and_own_model_hint(self):
        client = _StatusClient(
            httpx.Response(404, json={"error": {"message": "The model does not exist"}})
        )

        with self.assertRaises(HTTPException) as ctx:
            await post_openai_chat(client, "key", {})

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("OPENAI_MODEL", ctx.exception.detail)
        self.assertNotIn("GEMINI_MODEL", ctx.exception.detail)
        self.assertIn("does not exist", ctx.exception.detail)

    async def test_openai_401_reports_bad_key_and_does_not_retry(self):
        client = _StatusClient(httpx.Response(401, json={"error": {"message": "bad key"}}))

        with self.assertRaises(HTTPException) as ctx:
            await post_openai_chat(client, "key", {})

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("OpenAI", ctx.exception.detail)
        self.assertEqual(len(client.calls), 1, "auth failures must fail fast, not retry")

    async def test_openai_429_maps_to_rate_limit(self):
        client = _StatusClient(httpx.Response(429, json={}))

        with self.assertRaises(HTTPException) as ctx:
            await post_openai_chat(client, "key", {})

        self.assertEqual(ctx.exception.status_code, 429)

    async def test_openai_uses_bearer_auth_on_chat_completions(self):
        client = _StatusClient(
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
        )

        with patch.dict("os.environ", {}, clear=True):
            text = await OpenAIVisionProvider().generate(
                client, "sk-test", [text_part("hello")]
            )

        self.assertEqual(text, "ok")
        self.assertEqual(client.calls[0]["url"], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(client.calls[0]["headers"]["Authorization"], "Bearer sk-test")

    async def test_gemini_still_uses_goog_header_and_generate_content_url(self):
        client = _StatusClient(
            httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
            )
        )

        with patch.dict("os.environ", {}, clear=True):
            text = await GeminiVisionProvider().generate(
                client, "goog-key", [text_part("hello")]
            )

        self.assertEqual(text, "ok")
        self.assertIn(":generateContent", client.calls[0]["url"])
        self.assertEqual(client.calls[0]["headers"]["x-goog-api-key"], "goog-key")
        self.assertNotIn("Authorization", client.calls[0]["headers"])


if __name__ == "__main__":
    unittest.main()
