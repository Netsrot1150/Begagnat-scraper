"""eBay Browse API source.

The response shape is taken from eBay's published OpenAPI contract for
buy/browse/v1, so the field names are not guesses. The flow has still never run
against the live API - there were no credentials available - which is exactly
why these tests pin the request shape as tightly as the parsing.
"""

import json

import pytest

from begagnat.query import Criteria
from begagnat.sources import build, resolve
from begagnat.sources.base import SourceError
from begagnat.sources.ebay import EbaySource
from begagnat.sources.ebay_api import EbayApiSource

CRITERIA = Criteria(query="tesla p100", limit=60)
CREDENTIALS = {"client_id": "id-123", "client_secret": "secret-456"}


class ApiFetcher:
    """Answers the token call and the search call, and records both."""

    def __init__(self, body: str, *, token_status: int = 200, expires_in: int = 7200):
        self.body = body
        self.token_status = token_status
        self.expires_in = expires_in
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if "identity/v1/oauth2/token" in url:
            return _Response(
                json.dumps({"access_token": "tok-abc", "expires_in": self.expires_in}),
                self.token_status,
            )
        return _Response(self.body, 200)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def fetcher(fixture_text):
    return ApiFetcher(fixture_text("ebay_browse_search.json"))


# ------------------------------------------------------------------ parsing


def test_the_api_source_reads_price_shipping_and_country(fetcher):
    listings = list(EbayApiSource(fetcher, CREDENTIALS).search(CRITERIA))

    # The item with an empty title is dropped rather than stored as "".
    assert len(listings) == 2

    first = listings[0]
    assert first.source == "ebay"
    assert first.external_id == "335123456789", "the legacy id is the stable one"
    assert first.title == "Nvidia Tesla P100 16GB HBM2 PCIe GPU Accelerator"
    assert (first.price, first.currency) == (129.99, "USD")
    assert first.shipping == 32.0
    assert first.raw["country"] == "CN"
    assert first.seller == "gpu_liquidator"
    assert "?" not in first.url, "the tracking query string is stripped"


def test_an_auction_reports_its_current_bid(fetcher):
    """`price` is absent on a running auction; the current bid is what exists."""
    second = list(EbayApiSource(fetcher, CREDENTIALS).search(CRITERIA))[1]

    assert (second.price, second.currency) == (189.0, "EUR")
    assert second.raw["is_auction"] is True
    assert second.raw["bid_count"] == 7
    assert second.raw["country"] == "DE"
    assert second.location == "Berlin, Berlin, DE"


def test_a_missing_shipping_block_is_not_a_zero(fetcher):
    """Unknown shipping must stay None - zero would understate the landed cost."""
    second = list(EbayApiSource(fetcher, CREDENTIALS).search(CRITERIA))[1]
    assert second.shipping is None


# ------------------------------------------------------------ request shape


def test_the_token_is_fetched_once_and_then_reused(fetcher):
    source = EbayApiSource(fetcher, CREDENTIALS)
    list(source.search(CRITERIA))
    list(source.search(CRITERIA))

    token_calls = [c for c in fetcher.calls if "oauth2/token" in c[1]]
    assert len(token_calls) == 1, "a cached token must not be re-bought per search"

    method, _, kwargs = token_calls[0]
    assert method == "POST"
    assert kwargs["data"]["grant_type"] == "client_credentials"
    assert kwargs["headers"]["Authorization"].startswith("Basic ")


def test_an_expired_token_is_refreshed(fixture_text):
    """eBay's expiry is honoured; a stale bearer would 401 the whole run."""
    fetcher = ApiFetcher(fixture_text("ebay_browse_search.json"), expires_in=0)
    source = EbayApiSource(fetcher, CREDENTIALS)
    list(source.search(CRITERIA))
    list(source.search(CRITERIA))

    assert len([c for c in fetcher.calls if "oauth2/token" in c[1]]) == 2


def test_the_search_call_carries_the_marketplace_and_the_used_filter(fetcher):
    list(EbayApiSource(fetcher, {**CREDENTIALS, "domain": "ebay.de"}).search(CRITERIA))
    _, url, kwargs = [c for c in fetcher.calls if "item_summary/search" in c[1]][0]

    assert url.endswith("/buy/browse/v1/item_summary/search")
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_DE"
    assert kwargs["headers"]["Authorization"] == "Bearer tok-abc"
    assert kwargs["params"]["q"] == "tesla p100"
    assert "USED" in kwargs["params"]["filter"]
    assert "NEW" not in kwargs["params"]["filter"]


def test_a_region_becomes_an_item_location_filter(fetcher):
    list(EbayApiSource(fetcher, CREDENTIALS).search(Criteria(query="p100", region="DE")))
    _, _, kwargs = [c for c in fetcher.calls if "item_summary/search" in c[1]][0]
    assert "itemLocationCountry:DE" in kwargs["params"]["filter"]


def test_bad_credentials_fail_loudly_but_only_for_this_source(fixture_text):
    fetcher = ApiFetcher(fixture_text("ebay_browse_search.json"), token_status=401)
    with pytest.raises(SourceError, match="EBAY_CLIENT_ID"):
        list(EbayApiSource(fetcher, CREDENTIALS).search(CRITERIA))


# --------------------------------------------------------------- selection


def test_the_api_is_chosen_only_when_credentials_are_present(monkeypatch):
    monkeypatch.delenv("EBAY_CLIENT_ID", raising=False)
    monkeypatch.delenv("EBAY_CLIENT_SECRET", raising=False)

    assert resolve("ebay", {}) is EbaySource
    assert resolve("ebay", CREDENTIALS) is EbayApiSource

    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    assert resolve("ebay", {}) is EbayApiSource


def test_both_ebay_sources_share_one_name(fetcher, monkeypatch):
    """A listing must keep the same key whichever way it was found."""
    monkeypatch.setenv("EBAY_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "secret")
    built = build(["ebay"], fetcher, {})
    assert isinstance(built[0], EbayApiSource)
    assert built[0].name == EbaySource.name == "ebay"


def test_an_unknown_source_still_raises(fetcher):
    with pytest.raises(SourceError, match="unknown source"):
        build(["blipmarket"], fetcher, {})


# ------------------------------------------------------- credential handling


def test_credentials_come_from_the_config_file_not_the_command_line(tmp_path):
    """Secrets belong in a file, not in shell history or in `ps`."""
    from begagnat.config import Config

    config = Config.from_dict({
        "source_options": {"ebay": {"client_id": "id-from-file",
                                    "client_secret": "secret-from-file"}},
    })

    from begagnat.cli import build_parser, source_options

    args = build_parser().parse_args(["search", "tesla p100"])
    options = source_options(args, config)

    assert options["ebay"]["client_id"] == "id-from-file"
    assert options["ebay"]["client_secret"] == "secret-from-file"


def test_a_command_line_flag_still_beats_the_config_file():
    from begagnat.cli import build_parser, source_options
    from begagnat.config import Config

    config = Config.from_dict({"source_options": {"ebay": {"domain": "ebay.de",
                                                           "client_id": "keep-me",
                                                           "client_secret": "keep-me-too"}}})

    from_file = source_options(build_parser().parse_args(["search", "p100"]), config)
    overridden = source_options(
        build_parser().parse_args(["search", "p100", "--ebay-domain", "ebay.co.uk"]), config
    )

    assert from_file["ebay"]["domain"] == "ebay.de", "no flag means the file wins"
    assert overridden["ebay"]["domain"] == "ebay.co.uk"
    assert overridden["ebay"]["client_id"] == "keep-me", "a flag must not wipe the rest"


def test_an_oauth_response_is_never_written_to_the_debug_dump(tmp_path):
    """--save-html must not put a bearer token on disk."""
    from begagnat.http import Fetcher

    fetcher = Fetcher(cache_dir=tmp_path)

    class Response:
        text = '{"access_token": "v^1.1#i^1#SECRET", "expires_in": 7200}'

    fetcher._maybe_cache("https://api.ebay.com/identity/v1/oauth2/token", Response())
    assert list(tmp_path.glob("*")) == []

    fetcher._maybe_cache("https://api.ebay.com/buy/browse/v1/item_summary/search", Response())
    assert len(list(tmp_path.glob("*"))) == 1, "ordinary responses are still dumped"
