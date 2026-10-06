import pytest

from begagnat.fx import Fx, landed_cost_sek


def test_conversion_and_override():
    fx = Fx.default()
    assert fx.to_sek(100, "SEK") == 100
    assert fx.to_sek(10, "USD") == pytest.approx(97.0)
    assert fx.override({"usd": 11.0}).to_sek(10, "USD") == pytest.approx(110.0)


def test_unknown_currency_is_explicit():
    with pytest.raises(KeyError):
        Fx.default().to_sek(10, "JPY")


def test_eu_purchase_has_no_import_cost():
    total, import_cost = landed_cost_sek(1000, 100, country="DE")
    assert (total, import_cost) == (1100, 0.0)


def test_non_eu_purchase_adds_vat_and_clearance():
    total, import_cost = landed_cost_sek(1000, 100, country="CN")
    assert import_cost == pytest.approx(1100 * 0.25 + 125)
    assert total == pytest.approx(1100 + import_cost)


def test_unknown_country_is_treated_as_import():
    _, import_cost = landed_cost_sek(1000, 0, country=None)
    assert import_cost > 0
