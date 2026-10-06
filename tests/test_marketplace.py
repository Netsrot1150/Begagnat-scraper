"""Facebook Marketplace: URL building, block detection and tile parsing.

No browser and no network - `_parse` is pure. The Swedish-UI labels below were
captured live, logged out, on 2026-09-21; the English "SEK1,400" shape comes
from danyk20/facebook-marketplace-scraper (live-tested 2026-07).
"""

from urllib.parse import parse_qs, urlparse

import pytest

from begagnat.query import Criteria
from begagnat.sources.base import SourceError
from begagnat.sources.marketplace import (
    MarketplaceSource,
    blocked_reason,
    build_search_url,
    parse_price,
)


def _tile(item_id, label=None, body=""):
    aria = f' aria-label="{label}"' if label is not None else ""
    return f'<a href="/marketplace/item/{item_id}/?ref=search"{aria}>{body}</a>'


def _parse(*tiles):
    return list(MarketplaceSource(None)._parse("<div>" + "".join(tiles) + "</div>"))


def test_the_query_is_url_encoded_and_the_radius_is_capped():
    url = build_search_url("tesla p100 16gb & fläkt", city="sundsvall", radius_km=900)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.path == "/marketplace/sundsvall/search"
    assert params["query"] == ["tesla p100 16gb & fläkt"]
    assert params["radius"] == ["500"]
    assert params["sortBy"] == ["creation_time_descend"]


@pytest.mark.parametrize("url", [
    "https://www.facebook.com/login/?next=%2Fmarketplace%2F",
    "https://www.facebook.com/checkpoint/1501092823525282/",
    "https://www.facebook.com/two_step_verification/authentication/",
])
def test_a_login_redirect_is_reported_not_read_as_zero_results(url):
    assert "login-marketplace" in blocked_reason(url)


def test_the_eu_consent_screen_is_reported_and_never_clicked():
    reason = blocked_reason("https://www.facebook.com/privacy/consent/?flow=fb_dma_marketplace")
    assert "consent" in reason
    assert blocked_reason("https://www.facebook.com/marketplace/sundsvall/search?query=x") is None


def test_an_unknown_city_slug_is_reported_not_searched_elsewhere():
    reason = blocked_reason("https://www.facebook.com/marketplace/category/search/?query=x")
    assert "110103992352737" in reason


@pytest.mark.parametrize("token,expected", [
    ("1 400 kr", (1400, "SEK")), ("389 900 kr", (389900, "SEK")), ("SEK1,400", (1400, "SEK")),
    ("15 000 US$", (15000, "USD")), ("Gratis", (None, "SEK")), ("Free", (None, "SEK")),
])
def test_prices_parse_in_either_ui_language_and_currency(token, expected):
    assert parse_price(token) == expected


def test_a_swedish_tile_reads_title_price_and_place():
    [listing] = _parse(_tile(111, "Tesla P100 16GB, 1 400 kr, Sundsvall, Västernorrland, Annons 111",
                             '<img src="https://scontent.example/p.jpg">'))
    assert listing.external_id == "111"
    assert listing.title == "Tesla P100 16GB"
    assert listing.price == 1400
    assert listing.location == "Sundsvall, Västernorrland"
    assert listing.url == "https://www.facebook.com/marketplace/item/111"
    assert listing.image_url == "https://scontent.example/p.jpg"


def test_an_english_price_comma_does_not_shift_the_fields():
    [listing] = _parse(_tile(222, "RTX 3090 24GB, SEK7,500, Härnösand, Listing 222"))
    assert listing.title == "RTX 3090 24GB"
    assert listing.price == 7500
    assert listing.location == "Härnösand"


def test_a_title_with_commas_stays_whole():
    [listing] = _parse(_tile(333, "Dator, i7, 32GB, 3 500 kr, Timrå, Västernorrland, Annons 333"))
    assert listing.title == "Dator, i7, 32GB"
    assert listing.price == 3500


def test_without_an_aria_label_the_visible_text_is_used():
    body = "<span>2 900 kr</span><span>Dell Precision T5810 64GB</span><span>Sundsvall</span>"
    [listing] = _parse(_tile(444, None, body))
    assert listing.title == "Dell Precision T5810 64GB"
    assert listing.price == 2900
    assert listing.location == "Sundsvall"


def test_duplicate_tiles_and_non_item_links_are_dropped():
    listings = _parse(
        _tile(555, "Z690 D4, 900 kr, Sundsvall, Annons 555"),
        _tile(555, "Z690 D4, 900 kr, Sundsvall, Annons 555"),
        '<a href="/marketplace/you/selling">Sälj</a>',
    )
    assert [l.external_id for l in listings] == ["555"]


def test_live_swedish_labels_parse():
    listings = _parse(
        _tile(28370176232666429, "Interiörmattor Tesla model 3, 400 kr, Vällingby, AB, säljinlägg 28370176232666429"),
        _tile(1091884443333577, "Tesla model s \nStrålkastare 2017, 500 kr, Märsta, AB, säljinlägg 1091884443333577"),
        _tile(1616461353395980, "2015 Tesla model s, 15 000 US$, Pleasanton, CA, säljinlägg 1616461353395980"),
    )
    assert [(l.title, l.price, l.currency, l.location) for l in listings] == [
        ("Interiörmattor Tesla model 3", 400, "SEK", "Vällingby, AB"),
        ("Tesla model s Strålkastare 2017", 500, "SEK", "Märsta, AB"),
        ("2015 Tesla model s", 15000, "USD", "Pleasanton, CA"),
    ]


def test_a_badge_is_never_taken_for_the_title():
    # Live tile text, logged out: the badge is as long as the title it sits above.
    body = ("<span>Nyligen publicerat</span><span>15 000 US$</span>"
            "<span>2015 Tesla model s</span><span>Pleasanton, CA</span>")
    [listing] = _parse(_tile(1, None, body))
    assert listing.title == "2015 Tesla model s"
    assert (listing.price, listing.currency) == (15000, "USD")


def test_a_price_cut_keeps_the_place_clean_and_remembers_the_old_price():
    # Live label, 2026-09-21.
    [listing] = _parse(_tile(29140435805543150,
        "Speldator(Ryzen5/RTX), 5\u00a0500\u00a0kr, nedsatt från 6\u00a0500\u00a0kr, Härnösand, Y, säljinlägg 29140435805543150"))
    assert (listing.title, listing.price, listing.location) == ("Speldator(Ryzen5/RTX)", 5500, "Härnösand, Y")
    assert listing.raw["previous_price"] == 6500


def test_a_listing_without_a_title_is_skipped_not_named_after_its_town():
    # Live tile: the seller left the title empty; the only text is price and place.
    tile = _tile(2021518088550437, ", 1\u00a0200\u00a0kr, Sundsvall, Y, säljinlägg 2021518088550437",
                 "<span>1 200 kr</span><span>Sundsvall, Y</span>")
    assert _parse(tile) == []
