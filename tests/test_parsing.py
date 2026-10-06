from datetime import timezone

import pytest

from begagnat.parsing import parse_price, parse_timestamp


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("1 234 kr", 1234.0, "SEK"),
        ("1 400 kr", 1400.0, "SEK"),
        ("2.499:-", 2499.0, "SEK"),
        ("US $199.00", 199.0, "USD"),
        ("EUR 189,00", 189.0, "EUR"),
        ("£1,299.99", 1299.99, "GBP"),
        ("Utropspris 950 kr", 950.0, "SEK"),
        ("", None, "SEK"),
        ("Ring för pris", None, "SEK"),
    ],
)
def test_parse_price(text, amount, currency):
    assert parse_price(text) == (amount, currency)


def test_parse_timestamp_accepts_iso_and_epoch():
    iso = parse_timestamp("2026-09-01T08:12:00Z")
    assert iso.year == 2026 and iso.tzinfo is not None
    epoch = parse_timestamp(1756713600)
    assert epoch.tzinfo == timezone.utc
    assert parse_timestamp(1756713600000).year == epoch.year
    assert parse_timestamp(None) is None


@pytest.mark.parametrize(
    "text,amount,currency",
    [
        ("Nvidia Tesla V100 16GB PCIe 3 400 kr", 3400.0, "SEK"),
        ("RTX A4000 16GB 4 950 kr", 4950.0, "SEK"),
        ("US $199.00 + shipping", 199.0, "USD"),
        ("EUR 189,00", 189.0, "EUR"),
        ("2.499:- fast pris", 2499.0, "SEK"),
        # A bare model number must never be read as a price.
        ("Tesla P100 16GB", None, "SEK"),
        ("RTX 3090 24GB", None, "SEK"),
    ],
)
def test_find_price_requires_a_currency_marker(text, amount, currency):
    from begagnat.parsing import find_price

    assert find_price(text) == (amount, currency)
