from decimal import Decimal, InvalidOperation

SUPPORTED_CURRENCIES = {"EUR", "USD", "GBP"}

#: Display symbol per supported currency. Amounts are stored in EUR, so EUR is
#: also the base for every conversion.
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}
BASE_CURRENCY = "EUR"


def normalize_currency(value: str | None, default: str = BASE_CURRENCY) -> str:
    """Coerce a currency code to a supported one, falling back to `default`."""
    currency = (value or "").strip().upper()
    return currency if currency in SUPPORTED_CURRENCIES else default


def currency_symbol(value: str | None) -> str:
    """Symbol for a supported currency, falling back to the base currency's."""
    return CURRENCY_SYMBOLS[normalize_currency(value)]

# Only used when Frankfurter cannot be reached. Deliberately coarse: these are
# order-of-magnitude sane, not current. Every ordered pair of SUPPORTED_CURRENCIES
# must have an entry, which test_fallback_rates_cover_every_supported_pair enforces.
FALLBACK_RATES = {
    ("EUR", "USD"): 1.1,
    ("USD", "EUR"): 0.91,
    ("EUR", "GBP"): 0.85,
    ("GBP", "EUR"): 1.18,
    ("USD", "GBP"): 0.77,
    ("GBP", "USD"): 1.30,
}


class ExchangeRateError(ValueError):
    pass


def normalize_currency_pair(from_currency: str | None, to_currency: str | None) -> tuple[str, str]:
    source = (from_currency or "").strip().upper()
    target = (to_currency or "").strip().upper()
    if source not in SUPPORTED_CURRENCIES or target not in SUPPORTED_CURRENCIES:
        raise ExchangeRateError("unsupported currency pair")
    return source, target


def fallback_exchange_rate(from_currency: str, to_currency: str) -> float:
    if from_currency == to_currency:
        return 1.0
    return FALLBACK_RATES[(from_currency, to_currency)]


def _parse_positive_rate(raw_rate) -> float:
    try:
        rate = Decimal(str(raw_rate))
    except (InvalidOperation, TypeError):
        raise ExchangeRateError("missing exchange rate") from None
    if not rate.is_finite() or rate <= 0:
        raise ExchangeRateError("invalid exchange rate")
    return float(rate)


def parse_frankfurter_v2_rate(payload: dict) -> float:
    return _parse_positive_rate(payload.get("rate"))
