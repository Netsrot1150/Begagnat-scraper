"""Tradera REST API v4 source.

The fixture is a live capture from 2026-09-05 (queries "3090", "tesla p40" and
"grafikkort", one item of each `itemType` Tradera returned, descriptions
trimmed). Every price assertion below is therefore against real numbers, not
invented ones.
"""

import json

import pytest

from begagnat.models import Listing
from begagnat.query import Criteria, sort_key
from begagnat.sources import build, resolve
from begagnat.sources.base import SourceError
from begagnat.sources.tradera import TraderaSource
from begagnat.sources.tradera_api import MAX_PAGES, TraderaApiSource

CRITERIA = Criteria(query="3090", limit=60)
CREDENTIALS = {"app_id": "1234", "app_key": "key-abcdef"}


@pytest.fixture
def payload(fixture_text):
    return fixture_text("tradera_api_search.json")


@pytest.fixture
def source(fake_fetcher, payload):
    fetcher = fake_fetcher({"api.tradera.com/v4/search": payload})
    return TraderaApiSource(fetcher, dict(CREDENTIALS))


def find(listings, needle):
    return next(l for l in listings if needle in l.title)


# ------------------------------------------------------------------ wiring


def test_api_is_used_when_credentials_are_present():
    assert resolve("tradera", dict(CREDENTIALS)) is TraderaApiSource


def test_html_scraper_is_the_fallback():
    assert resolve("tradera", {}) is TraderaSource
    assert resolve("tradera", {"app_id": "only-half"}) is TraderaSource


def test_both_implementations_share_the_source_name(fake_fetcher, payload):
    api = build(["tradera"], fake_fetcher({"v4/search": payload}), {"tradera": CREDENTIALS})
    assert api[0].name == "tradera" == TraderaSource.name


def test_credentials_come_from_the_environment_too(monkeypatch, fake_fetcher, payload):
    monkeypatch.setenv("TRADERA_APP_ID", "9999")
    monkeypatch.setenv("TRADERA_APP_KEY", "env-key")
    assert resolve("tradera", {}) is TraderaApiSource
    source = TraderaApiSource(fake_fetcher({"v4/search": payload}), {})
    assert source.app_id == "9999"


def test_missing_credentials_are_a_source_error(fake_fetcher, payload):
    with pytest.raises(SourceError, match="TRADERA_APP_ID"):
        TraderaApiSource(fake_fetcher({"v4/search": payload}), {})


# ----------------------------------------------------------------- request


def test_credentials_are_sent_as_headers_not_query_params(source):
    list(source.search(CRITERIA))
    url, params = source.fetcher.calls[0]
    assert "app_id" not in params and "app_key" not in params
    assert source.headers["X-App-Id"] == "1234"
    assert source.headers["X-App-Key"] == "key-abcdef"


def test_search_sends_the_query_and_a_valid_sort(source):
    list(source.search(CRITERIA))
    _, params = source.fetcher.calls[0]
    assert params["query"] == "3090"
    assert params["orderBy"] == "Relevance"
    assert params["pageNumber"] == 1


def test_an_unknown_sort_fails_before_the_request(fake_fetcher, payload):
    fetcher = fake_fetcher({"v4/search": payload})
    source = TraderaApiSource(fetcher, {**CREDENTIALS, "sort": "AddedOnDescending"})
    # The API answers 400 OrderByValidationError; better to say so ourselves.
    with pytest.raises(SourceError, match="unknown Tradera sort"):
        list(source.search(CRITERIA))
    assert fetcher.calls == []


def test_a_401_names_the_config_key(fake_fetcher, payload):
    class Unauthorised(type(fake_fetcher({"x": "{}"}))):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            response.status_code = 401
            return response

    source = TraderaApiSource(Unauthorised({"v4/search": payload}), dict(CREDENTIALS))
    with pytest.raises(SourceError, match="source_options.tradera"):
        list(source.search(CRITERIA))


# --------------------------------------------------------------- pagination


def test_pagination_walks_pages_until_the_limit(fake_fetcher, payload):
    """The whole point of the API: the HTML page stopped at 80 listings."""
    items = json.loads(payload)["items"]

    def page(params):
        number = params["pageNumber"]
        return json.dumps({
            "totalNumberOfItems": 500,
            "totalNumberOfPages": 10,
            # Distinct ids per page, so a page that repeats itself is visible.
            "items": [{**item, "id": item["id"] * 100 + number} for item in items],
            "errors": [],
        })

    fetcher = fake_fetcher({"v4/search": page})
    source = TraderaApiSource(fetcher, dict(CREDENTIALS))
    listings = list(source.search(Criteria(query="3090", limit=20)))

    assert len(listings) == 20
    assert len({l.external_id for l in listings}) == 20
    assert [params["pageNumber"] for _, params in fetcher.calls] == [1, 2, 3]


def test_pagination_stops_at_the_last_page(fake_fetcher, payload):
    body = json.dumps({**json.loads(payload), "totalNumberOfPages": 1})
    fetcher = fake_fetcher({"v4/search": body})
    source = TraderaApiSource(fetcher, dict(CREDENTIALS))
    list(source.search(Criteria(query="3090", limit=500)))
    assert len(fetcher.calls) == 1


def test_pagination_has_a_hard_stop(fake_fetcher, payload):
    """A broad query must not walk every page Tradera is willing to serve."""
    items = json.loads(payload)["items"]

    def page(params):
        number = params["pageNumber"]
        return json.dumps({
            "totalNumberOfPages": 9999,
            "items": [{**item, "id": item["id"] * 10000 + number} for item in items],
            "errors": [],
        })

    fetcher = fake_fetcher({"v4/search": page})
    source = TraderaApiSource(fetcher, dict(CREDENTIALS))
    list(source.search(Criteria(query="dator", limit=10_000)))
    assert len(fetcher.calls) == MAX_PAGES


# ------------------------------------------------------------------ parsing


def test_a_fixed_price_listing_is_priced_at_its_buy_now(source):
    listing = find(list(source.search(CRITERIA)), "GIGABYTE RTX 3090 GAMING OC")
    assert listing.price == 12500.0
    assert listing.currency == "SEK"
    assert listing.auction == {}
    assert listing.price_is_provisional is False


def test_an_auction_is_priced_at_the_standing_bid_and_marked(source):
    listing = find(list(source.search(CRITERIA)), "MSI GeForce RTX 3090 SUPRIM X")
    assert listing.price == 10150.0
    assert listing.price_is_provisional is True
    assert listing.auction["bid_count"] == 2
    assert listing.auction["current_bid"] == 10150.0
    assert listing.auction["ends_at"]


def test_a_buy_now_on_an_auction_is_a_real_price(source):
    """`AuctionWithBuyItNow` still offers a cost you can pay today."""
    listing = find(list(source.search(CRITERIA)), "Kaiser Typ 3090")
    assert listing.price == 110.0             # the buy-now, not the 90 kr bid
    assert listing.auction["current_bid"] == 90.0
    assert listing.price_is_provisional is False


def test_zero_is_not_a_price(source):
    """Tradera sends buyItNowPrice 0 for "there is no buy-now"."""
    listing = find(list(source.search(CRITERIA)), "MSI GeForce RTX 3090 SUPRIM X")
    assert listing.auction["buy_now_price"] is None


def test_the_description_comes_along(source):
    """The markup never had this; the parser and the AI validator both want it."""
    listing = find(list(source.search(CRITERIA)), "GIGABYTE RTX 3090 GAMING OC")
    assert len(listing.description) > 20


def test_urls_are_https_and_unqueried(source):
    for listing in source.search(CRITERIA):
        assert listing.url.startswith("https://www.tradera.com/item/")
        assert "?" not in listing.url


def test_seller_attributes_are_flattened(source):
    listings = list(source.search(CRITERIA))
    attributes = {k for l in listings for k in l.raw["attributes"]}
    assert "condition" in attributes


def test_ids_match_the_html_scrapers_id_space(source):
    """Same key from either implementation, or every switch looks like new ads."""
    for listing in source.search(CRITERIA):
        assert listing.url.rsplit("/", 2)[-2] == listing.external_id


# ----------------------------------------------------------------- ranking


def _listing(price, **raw) -> Listing:
    listing = Listing(
        source="tradera", external_id=str(price), title="t", url="u",
        price=price, price_sek=price, raw=raw,
    )
    listing.score = 100.0 if raw.get("is_auction") else 10.0
    return listing


def test_a_running_auction_never_outranks_a_real_price():
    """A 1 kr opening bid used to be the best deal in the country."""
    bid = _listing(1.0, is_auction=True, bid_count=0)
    fixed = _listing(9000.0)
    assert sorted([bid, fixed], key=sort_key) == [fixed, bid]


def test_an_ended_auction_is_a_real_price():
    ended = _listing(4000.0, is_auction=True, bid_count=31, is_ended=True)
    assert ended.price_is_provisional is False
