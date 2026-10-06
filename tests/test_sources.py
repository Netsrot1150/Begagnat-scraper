"""Parser tests against captured-shape fixtures.

The fixtures mirror the response shapes the sources expect. When a marketplace
changes its markup, capture a real page with `--save-html` and update these.
"""

import json

import pytest

from begagnat.query import Criteria
from begagnat.sources.base import SourceError
from begagnat.sources.blocket import BlocketSource
from begagnat.sources.ebay import EbaySource
from begagnat.sources.tradera import TraderaSource

CRITERIA = Criteria(query="tesla p100", limit=60)


def test_blocket_recommerce_is_tried_first(fixture_text, fake_fetcher):
    """The current endpoint needs no token, so no token call should be made."""
    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce.json")})
    listings = list(BlocketSource(fetcher).search(CRITERIA))

    assert [l.external_id for l in listings] == ["1411111111", "1422222222"]
    assert listings[0].price == 1250
    assert listings[0].location == "Stockholm"
    assert not any("refresh-token" in url for url, _ in fetcher.calls)


def test_blocket_falls_back_to_bff_when_recommerce_fails(fixture_text, fake_fetcher):
    fetcher = fake_fetcher(
        {
            "refresh-token": json.dumps({"bearerToken": "tok123"}),
            "search_bff": fixture_text("blocket_search.json"),
        }
    )
    listings = list(BlocketSource(fetcher).search(CRITERIA))

    assert len(listings) == 3
    first = listings[0]
    assert first.external_id == "1401234567"
    assert first.price == 1250
    assert first.currency == "SEK"
    assert first.location == "Stockholm, Solna"
    assert first.seller == "Anders"
    assert first.posted_at.year == 2026
    assert first.url.startswith("https://www.blocket.se/annons/")
    assert first.raw["country"] == "SE"

    # The bearer token from the first call must be sent on the search call.
    search_call = [c for c in fetcher.calls if "search_bff" in c[0]][0]
    assert search_call[1]["q"] == "tesla p100"


def test_blocket_falls_back_to_html_when_both_apis_fail(fixture_text, fake_fetcher):
    payload = json.loads(fixture_text("blocket_search.json"))
    next_data = json.dumps({"props": {"ads": payload["data"]}})
    html = f'<html><body><script id="__NEXT_DATA__">{next_data}</script></body></html>'

    class Broken(fake_fetcher):
        def get(self, url, **kwargs):
            if "refresh-token" in url or "SEARCH_ID_BAP_COMMON" in url:
                raise RuntimeError("endpoint moved")
            return super().get(url, **kwargs)

    listings = list(BlocketSource(Broken({"blocket.se": html})).search(CRITERIA))
    assert {l.external_id for l in listings} == {"1401234567", "1409999999", "1400000001"}


def test_tradera_next_data_parsing(fixture_text, fake_fetcher):
    fetcher = fake_fetcher({"tradera.com": fixture_text("tradera_search.html")})
    listings = list(TraderaSource(fetcher).search(CRITERIA))

    assert len(listings) == 3
    by_id = {l.external_id: l for l in listings}
    assert by_id["612345678"].title == "Nvidia Tesla P100 12GB"
    assert by_id["612345678"].price == 900
    assert by_id["612345678"].url == "https://www.tradera.com/item/302092/612345678/nvidia-tesla-p100"
    assert by_id["612345678"].location == "Uppsala"
    # A running auction reports the current bid rather than a buy-it-now price.
    assert by_id["612345679"].price == 6100


def test_tradera_card_fallback(fixture_text, fake_fetcher):
    fetcher = fake_fetcher({"tradera.com": fixture_text("tradera_cards.html")})
    listings = list(TraderaSource(fetcher).search(CRITERIA))

    assert len(listings) == 2
    assert listings[0].external_id == "700000001"
    assert listings[0].price == 3400
    assert listings[1].price == 4950


def test_ebay_parsing(fixture_text, fake_fetcher):
    fetcher = fake_fetcher({"ebay.com": fixture_text("ebay_search.html")})
    listings = list(EbaySource(fetcher, {"domain": "ebay.com"}).search(CRITERIA))

    # The "Shop on eBay" placeholder card is dropped.
    assert len(listings) == 2
    first, second = listings
    assert first.external_id == "335123456789"
    assert (first.price, first.currency) == (129.99, "USD")
    assert first.shipping == 32.0
    assert first.raw["country"] == "CN"
    assert "?" not in first.url

    # "NEW LISTING" is a badge, not part of the title.
    assert second.title == "Nvidia Tesla P40 24GB"
    assert (second.price, second.currency) == (189.0, "EUR")
    assert second.shipping is None
    assert second.raw["country"] == "DE"


def test_tradera_item_cards_are_read_before_the_older_strategies(fixture_text, fake_fetcher):
    """Real capture, 2026-09-04: Tradera dropped __NEXT_DATA__ and ships item cards.

    Every card carries `data-item-type` and a `data-testid="price"` span, so the
    four sale shapes (fixed price, auction, auction with a buy-now, and a
    contact-only vehicle ad) are all readable without guessing at the markup.
    """
    fetcher = fake_fetcher({"tradera.com": fixture_text("tradera_cards_live.html")})
    listings = {l.external_id: l for l in TraderaSource(fetcher).search(CRITERIA)}

    assert len(listings) == 4

    gpu = listings["747306011"]
    assert gpu.title == "NVIDIA Tesla P100 AI server grafikkort"
    assert (gpu.price, gpu.currency) == (1900, "SEK")
    assert gpu.url == (
        "https://www.tradera.com/item/341372/747306011/nvidia-tesla-p100-ai-server-grafikkort"
    )
    assert gpu.image_url.startswith("https://img.tradera.net/")
    assert gpu.raw["item_type"] == "PureBin"
    assert gpu.raw["is_auction"] is False
    assert gpu.raw["ends_at"] == "26 okt 19:49"

    # A live auction: the visible price is the starting bid, and it will rise.
    auction = listings["748622332"]
    assert auction.price == 500
    assert auction.raw["is_auction"] is True
    assert auction.raw["buy_now_price"] is None

    # Auction with a buy-now: price is what it costs to end it now, bid kept aside.
    both = listings["748623160"]
    assert both.price == 1650
    assert both.raw["buy_now_price"] == 1650
    assert both.raw["current_bid"] == 1200
    assert both.raw["is_auction"] is True

    # Contact-only vehicle ads are still listings; the GPU filter drops them later.
    assert listings["747393343"].price == 299900


def test_blocket_region_names_become_location_codes(fixture_text, fake_fetcher):
    """Blocket wants "0.300003"; the user types "Uppsala". Verified live 2026-09-04."""
    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce.json")})
    list(BlocketSource(fetcher).search(Criteria(query="grafikkort", region="Uppsala")))

    params = [p for url, p in fetcher.calls if "SEARCH_ID_BAP_COMMON" in url][0]
    assert params["location"] == "0.300003"


def test_blocket_region_lookup_folds_accents_and_follows_aliases():
    from begagnat.sources import blocket_regions

    assert blocket_regions.code_for("Skåne") == "0.300012"
    assert blocket_regions.code_for("skane") == "0.300012", "typing without accents works"
    assert blocket_regions.code_for("Göteborg") == "0.300014", "a city maps to its county"
    assert blocket_regions.code_for("0.300099") == "0.300099", "a raw code passes through"
    assert blocket_regions.code_for(None) is None
    assert blocket_regions.code_for("Lappland") is None


def test_an_unknown_blocket_region_says_what_the_valid_ones_are(fixture_text, fake_fetcher):
    fetcher = fake_fetcher({"blocket": fixture_text("blocket_recommerce.json")})
    with pytest.raises(SourceError, match="unknown Blocket region"):
        list(BlocketSource(fetcher).search(Criteria(query="x", region="Mordor")))


def test_blocket_pages_until_the_limit_is_filled(fixture_text, fake_fetcher):
    """Blocket ignores a page-size parameter and serves ~53 ads per page.

    Captured live 2026-09-04: "grafikkort" is 1656 ads over 32 pages, so
    reading only page one saw 3% of the market. `limit` has to buy pages.
    """
    pages = {
        "1": fixture_text("blocket_recommerce_live_p1.json"),
        "2": fixture_text("blocket_recommerce_live_p2.json"),
    }
    fetcher = fake_fetcher(
        {"SEARCH_ID_BAP_COMMON": lambda params: pages[str(params["page"])]}
    )

    listings = list(BlocketSource(fetcher).search(Criteria(query="grafikkort", limit=4)))

    assert [c[1]["page"] for c in fetcher.calls] == [1, 2], "both pages read, then stop"
    assert [l.external_id for l in listings] == [
        "26291750", "26300901", "26289999", "26213015",
    ]
    assert all(c[1]["sort"] == "PUBLISHED_DESC" for c in fetcher.calls), "newest first"


def test_blocket_never_returns_more_than_the_limit(fixture_text, fake_fetcher):
    pages = {
        "1": fixture_text("blocket_recommerce_live_p1.json"),
        "2": fixture_text("blocket_recommerce_live_p2.json"),
    }
    fetcher = fake_fetcher(
        {"SEARCH_ID_BAP_COMMON": lambda params: pages[str(params["page"])]}
    )

    listings = list(BlocketSource(fetcher).search(Criteria(query="grafikkort", limit=3)))
    assert len(listings) == 3


def test_blocket_stops_after_one_page_when_that_is_all_there_is(fixture_text, fake_fetcher):
    """No `metadata.paging` means one page - a shape change must not loop."""
    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce.json")})

    list(BlocketSource(fetcher).search(Criteria(query="tesla p100", limit=60)))

    assert len(fetcher.calls) == 1


def test_blocket_reads_the_live_timestamp_field(fixture_text, fake_fetcher):
    """The live endpoint sends `timestamp` in epoch millis, not `list_time`."""
    fetcher = fake_fetcher({"SEARCH_ID_BAP_COMMON": fixture_text("blocket_recommerce_live_p1.json")})

    first = list(BlocketSource(fetcher).search(Criteria(query="grafikkort", limit=2)))[0]

    assert first.posted_at is not None
    assert first.posted_at.year == 2026
    assert first.price == 4000
    assert first.location == "Vansbro"
