import unittest
from unittest.mock import patch

try:
    from services.telegram import _format_user_eur
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


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
    def __init__(self, currency):
        self._row = _Row(currency) if currency is not None else None

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._row)


@unittest.skipUnless(DEPS_AVAILABLE, "httpx is not installed in this lightweight test environment")
class TelegramCurrencyTests(unittest.TestCase):
    """Alerts used to special-case USD and label everything else as euro, so a
    GBP user was sent unconverted figures with the wrong symbol."""

    def _format(self, amount, currency, rate=0.85786):
        # Force the offline path so the test never depends on the network, then
        # assert against the fallback rate the code itself would use.
        with patch("services.telegram.httpx.Client", side_effect=OSError("offline")):
            return _format_user_eur(amount, db=_FakeDb(currency), user_id=1)

    def test_eur_user_gets_euro_unconverted(self):
        self.assertEqual(self._format(10.0, "EUR"), "€10.00")

    def test_gbp_user_gets_pounds_and_a_conversion(self):
        result = self._format(10.0, "GBP")
        self.assertTrue(result.startswith("£"), f"expected pounds, got {result!r}")
        self.assertNotEqual(result, "£10.00", "amount was labelled GBP but not converted")

    def test_usd_user_still_gets_dollars(self):
        result = self._format(10.0, "USD")
        self.assertTrue(result.startswith("$"), f"expected dollars, got {result!r}")

    def test_unsupported_currency_falls_back_to_the_base(self):
        self.assertEqual(self._format(10.0, "JPY"), "€10.00")

    def test_missing_setting_falls_back_to_the_base(self):
        self.assertEqual(self._format(10.0, None), "€10.00")

    def test_none_amount_is_treated_as_zero(self):
        self.assertEqual(self._format(None, "EUR"), "€0.00")


if __name__ == "__main__":
    unittest.main()
