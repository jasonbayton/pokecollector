import unittest
from unittest.mock import patch

try:
    from fastapi import HTTPException

    from api.export import _convert_eur, _normalize_currency
    from api.settings import (
        SECRET_KEYS,
        _coerce_setting_value,
        _default_currency,
        _mask_secret,
        _redact_secrets,
    )
    from services.scan_providers import ScanProvider
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False

skip_without_deps = unittest.skipUnless(
    DEPS_AVAILABLE, "FastAPI is not installed in this lightweight test environment"
)


class _Row:
    def __init__(self, value):
        self.value = value


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _FakeDb:
    """Stands in for a Session returning one user_settings row."""

    def __init__(self, row=None):
        self._row = row

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._row)


@skip_without_deps
class SecretRedactionTests(unittest.TestCase):
    def test_every_secret_is_blanked_whatever_its_source(self):
        settings = {k: "super-secret-value" for k in SECRET_KEYS}
        settings["trainer_name"] = "TRAINER"

        redacted = _redact_secrets(settings)

        for key in SECRET_KEYS:
            self.assertEqual(redacted[key], "", f"{key} was returned to the browser")
        self.assertEqual(redacted["trainer_name"], "TRAINER", "non-secrets must survive")

    def test_redaction_does_not_mutate_the_caller_dict(self):
        settings = {"openai_api_key": "sk-live"}
        _redact_secrets(settings)
        self.assertEqual(settings["openai_api_key"], "sk-live")

    def test_mask_shows_only_a_tail(self):
        masked = _mask_secret("sk-abcdefghijklmnop")
        self.assertTrue(masked.endswith("mnop"))
        self.assertNotIn("abcdefghij", masked)

    def test_short_secrets_reveal_nothing(self):
        self.assertNotIn("abc", _mask_secret("abc"))


@skip_without_deps
class SettingValidationTests(unittest.TestCase):
    def test_unknown_provider_is_rejected_not_silently_defaulted(self):
        with self.assertRaises(HTTPException) as ctx:
            _coerce_setting_value("scanner_provider", "llama-vision")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_known_providers_are_normalised(self):
        self.assertEqual(_coerce_setting_value("scanner_provider", " OpenAI "), "openai")

    def test_unsupported_currency_is_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            _coerce_setting_value("currency", "JPY")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_supported_currency_is_normalised(self):
        self.assertEqual(_coerce_setting_value("currency", " gbp "), "GBP")

    def test_secret_values_are_trimmed(self):
        self.assertEqual(_coerce_setting_value("openai_api_key", "  sk-live  "), "sk-live")

    def test_default_currency_falls_back_when_unsupported(self):
        with patch.dict("os.environ", {"DEFAULT_CURRENCY": "JPY"}):
            self.assertEqual(_default_currency(), "EUR")

    def test_default_currency_honours_a_supported_value(self):
        with patch.dict("os.environ", {"DEFAULT_CURRENCY": "gbp"}):
            self.assertEqual(_default_currency(), "GBP")


@skip_without_deps
class ProviderKeyPrecedenceTests(unittest.TestCase):
    def test_user_key_wins_over_environment(self):
        db = _FakeDb(_Row("sk-user"))
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env"}):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-user")

    def test_whitespace_only_key_does_not_shadow_the_environment(self):
        db = _FakeDb(_Row("   "))
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env"}):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-env")

    def test_user_key_is_trimmed(self):
        db = _FakeDb(_Row("  sk-user  "))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-user")

    def test_the_installation_key_is_used_when_the_user_has_none(self):
        # Setting the environment key is the operator's decision to provide one
        # for everybody; there is no separate opt-in.
        db = _FakeDb(None)
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env"}, clear=True):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-env")

    def test_a_users_own_key_wins_over_the_installation_key(self):
        db = _FakeDb(_Row("sk-user"))
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env"}, clear=True):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-user")

    def test_each_provider_reads_its_own_environment_variable(self):
        db = _FakeDb(None)
        env = {
            "GEMINI_API_KEY": "goog-key",
            "OPENAI_API_KEY": "sk-key",
        }
        with patch.dict("os.environ", env):
            self.assertEqual(ScanProvider("gemini").credential(db, 1), "goog-key")
            self.assertEqual(ScanProvider("openai").credential(db, 1), "sk-key")

    def test_no_installation_key_means_no_fallback(self):
        # The operator provides for everyone or for no one, and this is no one.
        db = _FakeDb(None)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(ScanProvider("openai").credential(db, 1), "")


@skip_without_deps
class SettingScopeTests(unittest.TestCase):
    """A key belongs to exactly one scope.

    ADMIN_ONLY_KEYS writes go to the global settings table and PER_USER_KEYS
    writes go to user_settings. A key in both is written to one table and read
    from the other, so the setting appears to save and is then ignored.
    """

    def test_the_two_scopes_do_not_overlap(self):
        from api.settings import ADMIN_ONLY_KEYS, PER_USER_KEYS

        overlap = ADMIN_ONLY_KEYS & PER_USER_KEYS
        self.assertEqual(overlap, set(), f"keys in both scopes: {sorted(overlap)}")

    def test_the_scanner_provider_is_per_user(self):
        from api.settings import ADMIN_ONLY_KEYS, PER_USER_KEYS
        from services.scan_providers import SCANNER_PROVIDER_SETTING

        self.assertIn(SCANNER_PROVIDER_SETTING, PER_USER_KEYS)
        self.assertNotIn(SCANNER_PROVIDER_SETTING, ADMIN_ONLY_KEYS)


@skip_without_deps
class ExportCurrencyTests(unittest.TestCase):
    def test_gbp_is_recognised_with_its_own_symbol(self):
        self.assertEqual(_normalize_currency("gbp"), ("GBP", "£"))

    def test_unsupported_currency_falls_back_to_the_base(self):
        self.assertEqual(_normalize_currency("JPY"), ("EUR", "€"))

    def test_base_currency_amounts_are_not_converted(self):
        self.assertEqual(_convert_eur(10.0, 0.85, "EUR"), 10.0)

    def test_other_currencies_apply_the_rate(self):
        self.assertAlmostEqual(_convert_eur(10.0, 0.85786, "GBP"), 8.5786, places=4)

    def test_none_stays_none(self):
        self.assertIsNone(_convert_eur(None, 0.85, "GBP"))


if __name__ == "__main__":
    unittest.main()
