import unittest

from itertools import product

from services.exchange_rates import (
    SUPPORTED_CURRENCIES,
    ExchangeRateError,
    fallback_exchange_rate,
    normalize_currency_pair,
    parse_frankfurter_v2_rate,
)


class ExchangeRateTests(unittest.TestCase):
    def test_normalizes_supported_currency_pair(self):
        self.assertEqual(normalize_currency_pair(" eur ", "usd"), ("EUR", "USD"))

    def test_rejects_unsupported_currency_pair(self):
        with self.assertRaises(ExchangeRateError):
            normalize_currency_pair("EUR", "JPY")

    def test_accepts_gbp(self):
        self.assertEqual(normalize_currency_pair(" gbp ", "eur"), ("GBP", "EUR"))
        self.assertEqual(normalize_currency_pair("EUR", "gbp"), ("EUR", "GBP"))

    def test_fallback_rates_are_available_for_supported_pairs(self):
        self.assertEqual(fallback_exchange_rate("EUR", "EUR"), 1.0)
        self.assertEqual(fallback_exchange_rate("EUR", "USD"), 1.1)
        self.assertEqual(fallback_exchange_rate("USD", "EUR"), 0.91)

    def test_fallback_rates_cover_every_supported_pair(self):
        # Adding a currency without its fallback pairs would otherwise only blow
        # up at runtime, and only when Frankfurter happens to be unreachable.
        for source, target in product(sorted(SUPPORTED_CURRENCIES), repeat=2):
            with self.subTest(pair=(source, target)):
                rate = fallback_exchange_rate(source, target)
                self.assertGreater(rate, 0)

    def test_parses_frankfurter_v2_rate(self):
        self.assertEqual(parse_frankfurter_v2_rate({"rate": 0.92}), 0.92)

    def test_rejects_missing_or_invalid_frankfurter_v2_rate(self):
        for payload in (
            {},
            {"rate": 0},
            {"rate": "nope"},
            {"rate": "NaN"},
            {"rate": "Infinity"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ExchangeRateError):
                    parse_frankfurter_v2_rate(payload)


if __name__ == "__main__":
    unittest.main()
