import pytest

from begagnat.fx import Fx
from begagnat.models import Listing
from begagnat.query import Criteria, analyse

FX = Fx.default()


def make(title, price=1000, currency="SEK", country="SE", **kw):
    return Listing(
        source="test", external_id=title[:12], title=title, url="http://x",
        price=price, currency=currency, raw={"country": country}, **kw
    )


def test_accepts_a_matching_listing():
    listing = analyse(make("Nvidia Tesla P100 16GB", 1250), Criteria(min_vram=16), FX)
    assert not listing.rejected_reason
    assert listing.gpu.name == "Tesla P100"
    assert listing.price_sek == 1250
    assert listing.kr_per_gb == pytest.approx(78.1, abs=0.1)
    assert listing.score > 0


def test_rejects_over_budget():
    listing = analyse(make("Tesla P100 16GB", 1800), Criteria(max_price=1400), FX)
    assert "over budget" in listing.rejected_reason


def test_rejects_too_little_vram():
    listing = analyse(make("Tesla P4 8GB", 500), Criteria(min_vram=16), FX)
    assert "8 GB VRAM" in listing.rejected_reason


def test_quantity_bundle_passes_a_vram_floor_it_would_fail_alone():
    listing = analyse(make("2 st Tesla P100 16GB", 2400), Criteria(min_vram=32), FX)
    assert not listing.rejected_reason
    assert listing.gpu.total_vram_gb == 32


def test_rejects_broken_and_accessories_by_default():
    assert "excluded" in analyse(make("Trasig RTX 3090"), Criteria(), FX).rejected_reason
    assert "excluded" in analyse(make("Kylare till Tesla P40"), Criteria(), FX).rejected_reason
    assert not analyse(make("Trasig RTX 3090"), Criteria(allow_broken=True), FX).rejected_reason


def test_rejects_unknown_gpu_unless_allowed():
    assert analyse(make("Snowboard 156cm"), Criteria(), FX).rejected_reason
    listing = analyse(make("Snowboard 156cm"), Criteria(require_known_gpu=False), FX)
    assert not listing.rejected_reason and listing.gpu is None


def test_min_tier_filters_out_weak_architectures():
    listing = analyse(make("Tesla M40 24GB", 900), Criteria(min_tier="good"), FX)
    assert "tier poor" in listing.rejected_reason


def test_kr_per_gb_cap():
    listing = analyse(make("RTX 3090 24GB", 9000), Criteria(max_kr_per_gb=200), FX)
    assert "kr/GB over limit" in listing.rejected_reason


def test_per_model_cap_scales_with_quantity():
    criteria = Criteria(per_model_max_price={"Tesla P100": 1400})
    assert analyse(make("Tesla P100 16GB", 1500), criteria, FX).rejected_reason
    assert not analyse(make("2 st Tesla P100 16GB", 2700), criteria, FX).rejected_reason


def test_non_eu_price_includes_import_cost():
    listing = analyse(
        make("Tesla P100 16GB", 129.99, currency="USD", country="CN", shipping=32.0),
        Criteria(),
        FX,
    )
    goods = FX.to_sek(129.99, "USD") + FX.to_sek(32.0, "USD")
    assert listing.import_cost_sek == pytest.approx(goods * 0.25 + 125, abs=0.5)
    assert listing.price_sek == pytest.approx(goods + listing.import_cost_sek, abs=0.5)


def test_eu_seller_pays_no_import_cost():
    listing = analyse(
        make("Tesla P40 24GB", 189.0, currency="EUR", country="DE"), Criteria(), FX
    )
    assert listing.import_cost_sek is None
    assert listing.price_sek == pytest.approx(FX.to_sek(189.0, "EUR"))


def test_listing_without_a_price_is_rejected_with_an_explanation():
    listing = analyse(make("Tesla P100 16GB", None), Criteria(), FX)
    assert listing.price_sek is None
    assert listing.score == 0.0
    assert "no price" in listing.rejected_reason


def test_a_rejected_listing_still_carries_its_price():
    # --show-rejected and the AI rescue both need the price of a dropped ad.
    listing = analyse(make("Cykel till salu", 1500), Criteria(), FX)
    assert listing.rejected_reason == "no known GPU in title"
    assert listing.price_sek == 1500


def test_scoring_prefers_bandwidth_at_equal_vram_and_price():
    p100 = analyse(make("Tesla P100 16GB", 1400), Criteria(), FX)   # 732 GB/s
    a770 = analyse(make("Intel Arc A770 16GB", 1400), Criteria(), FX)  # 560 GB/s, weaker stack
    assert p100.score > a770.score


def test_scoring_prefers_the_cheaper_of_two_identical_cards():
    cheap = analyse(make("Tesla P100 16GB", 1200), Criteria(), FX)
    dear = analyse(make("Tesla P100 16GB", 2400), Criteria(), FX)
    assert cheap.score == pytest.approx(dear.score * 2, rel=0.01)


def test_required_keyword():
    criteria = Criteria(keywords=["pcie"])
    assert analyse(make("Tesla P100 16GB SXM2"), criteria, FX).rejected_reason
    assert not analyse(make("Tesla P100 16GB PCIe"), criteria, FX).rejected_reason


def test_model_restriction():
    criteria = Criteria(models=["Tesla P100"])
    assert "not in --model filter" in analyse(make("RTX 3090 24GB"), criteria, FX).rejected_reason
