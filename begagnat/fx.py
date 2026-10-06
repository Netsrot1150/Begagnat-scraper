"""Currency conversion and landed-cost estimation for imports.

Rates are static on purpose: a scraper that dies because a rate API is down is
worse than one that is a few percent off. Override with --fx USD=11.2 or the
``fx`` block in the config file.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RATES_TO_SEK: dict[str, float] = {
    "SEK": 1.0,
    "EUR": 11.30,
    "USD": 9.70,
    "GBP": 13.20,
    "DKK": 1.52,
    "NOK": 0.95,
    "PLN": 2.65,
}

# Swedish VAT on imports. Consumer electronics carry 0% customs duty under the
# WTO ITA, but VAT applies to goods + shipping, and couriers add a clearance fee.
VAT_RATE = 0.25
CUSTOMS_CLEARANCE_SEK = 125.0

EU_ISO = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU",
    "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
}


@dataclass
class Fx:
    rates: dict[str, float]

    @classmethod
    def default(cls) -> "Fx":
        return cls(dict(DEFAULT_RATES_TO_SEK))

    def override(self, pairs: dict[str, float]) -> "Fx":
        rates = dict(self.rates)
        rates.update({k.upper(): float(v) for k, v in pairs.items()})
        return Fx(rates)

    def to_sek(self, amount: float | None, currency: str) -> float | None:
        if amount is None:
            return None
        rate = self.rates.get((currency or "SEK").upper())
        if rate is None:
            raise KeyError(f"no exchange rate for {currency}; pass --fx {currency}=<rate>")
        return amount * rate


def landed_cost_sek(
    goods_sek: float,
    shipping_sek: float = 0.0,
    *,
    country: str | None,
    apply_import_cost: bool = True,
) -> tuple[float, float]:
    """Return ``(total_sek, import_cost_sek)`` for a purchase.

    Anything shipped from outside the EU picks up 25% VAT on goods plus
    shipping, and a courier clearance fee. An unknown country is treated as
    non-EU, because that is the expensive assumption and the one you want to
    be warned about.
    """
    base = goods_sek + shipping_sek
    if not apply_import_cost:
        return base, 0.0
    iso = (country or "").strip().upper()
    if iso in EU_ISO:
        return base, 0.0
    import_cost = base * VAT_RATE + CUSTOMS_CLEARANCE_SEK
    return base + import_cost, import_cost
