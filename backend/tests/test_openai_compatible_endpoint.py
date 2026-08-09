import asyncio
import os
import unittest
from unittest.mock import patch

try:
    from services.vision_provider import (
        DEFAULT_OPENAI_BASE_URL,
        GeminiVisionProvider,
        OpenAIVisionProvider,
        openai_base_url,
        openai_chat_completions_url,
        post_openai_chat,
    )
    DEPS = True
except ModuleNotFoundError:
    DEPS = False


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class BaseUrlTests(unittest.TestCase):
    """Ollama, llama.cpp and LM Studio serve the same chat completions shape,
    so the endpoint has to be pointable somewhere else."""

    def test_it_defaults_to_the_hosted_api(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_BASE_URL", None)
            self.assertEqual(openai_base_url(), DEFAULT_OPENAI_BASE_URL)
            self.assertEqual(openai_chat_completions_url(), "https://api.openai.com/v1/chat/completions")

    def test_it_can_point_at_a_local_endpoint(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}):
            self.assertEqual(openai_chat_completions_url(), "http://127.0.0.1:11434/v1/chat/completions")

    def test_a_trailing_slash_does_not_double_up(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1/"}):
            self.assertEqual(openai_chat_completions_url(), "http://127.0.0.1:11434/v1/chat/completions")

    def test_blank_configuration_falls_back_rather_than_breaking(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "   "}):
            self.assertEqual(openai_base_url(), DEFAULT_OPENAI_BASE_URL)


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class ApiKeyRequirementTests(unittest.TestCase):
    """A self-hosted endpoint usually has no credential at all, so demanding one
    would make a local model impossible to use."""

    def test_the_hosted_api_still_needs_a_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_BASE_URL", None)
            self.assertTrue(OpenAIVisionProvider().requires_api_key())

    def test_a_local_endpoint_does_not(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}):
            self.assertFalse(OpenAIVisionProvider().requires_api_key())

    def test_gemini_always_needs_one(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}):
            self.assertTrue(GeminiVisionProvider().requires_api_key())


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class AuthHeaderTests(unittest.TestCase):
    """An empty key must mean no Authorization header, not an empty bearer
    token, which some servers reject outright."""

    def _headers_for(self, api_key):
        captured = {}

        async def fake_post(client, url, headers, payload, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            return None

        with patch("services.vision_provider._post_with_retries", fake_post):
            asyncio.run(post_openai_chat(None, api_key, {"model": "x"}))
        return captured

    def test_a_key_is_sent_when_there_is_one(self):
        captured = self._headers_for("sk-test")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-test")

    def test_no_header_is_sent_when_there_is_no_key(self):
        captured = self._headers_for("")
        self.assertNotIn("Authorization", captured["headers"])
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")

    def test_the_configured_endpoint_is_the_one_called(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://127.0.0.1:11434/v1"}):
            captured = self._headers_for("")
        self.assertEqual(captured["url"], "http://127.0.0.1:11434/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
