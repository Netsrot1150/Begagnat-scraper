"""Tradera REST API v4 - the official way past the 80-listing HTML cap.

The HTML scraper in `tradera.py` reads one rendered search page and stops at
whatever Tradera chose to render. The API pages through the whole result set
(50 per page, verified at 2995 items over 60 pages for "grafikkort") and hands
back three things the markup never had: the full `longDescription`, the bid
state, and the seller's own condition attribute.

Setup, once:

1. Register an application at https://api.tradera.com/register (free).
2. Put the Application ID and App Key in ~/.config/begagnat/config.yaml under
   `source_options.tradera` as `app_id` and `app_key`, or export
   TRADERA_APP_ID / TRADERA_APP_KEY.

`TraderaApiSource` is used automatically whenever those two are present;
without them the run falls back to `TraderaSource` and its HTML scraping.

Field names and semantics were verified live on 2026-09-05:

* `maxBid` is the current highest bid, and equals `nextBid` when `bidCount`
  is 0 - i.e. on a fresh auction it is the opening bid, not a price anyone
  has agreed to pay.
* `buyItNowPrice` is only present on `ShopItem` and `AuctionWithBuyItNow`.
* `itemType` is a *string* here ("Auction"), while /v4/items/{id} returns the
  same field as an integer. Only the search shape is used.
* The API declares `charset=utf-8` and means it; the mojibake you may see is
  a cp1252 console, not the payload.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from ..models import Listing
from ..query import Criteria
from .base import Source, SourceError

log = logging.getLogger(__name__)

BASE = "https://api.tradera.com/v4"
SEARCH_URL = f"{BASE}/search"
WEB_BASE = "https://www.tradera.com"

#: The API's own page size. Not a parameter - `pageNumber` is all there is.
PAGE_SIZE = 50
#: A stop so a broad query cannot walk all 60 pages of everything.
MAX_PAGES = 20

#: Rejected with 400 OrderByValidationError if it is not one of these.
SORT_ORDERS = (
    "Relevance",
    "EndDateAscending",
    "EndDateDescending",
    "PriceAscending",
    "PriceDescending",
    "BidsAscending",
    "BidsDescending",
)
DEFAULT_SORT = "Relevance"

#: Item types where the visible amount is a bid, not a cost.
_AUCTION_TYPES = {"Auction", "AuctionWithBuyItNow"}


def credentials(options: dict | None = None) -> tuple[str, str] | None:
    """App id and key from the source options, else from the environment."""
    options = options or {}
    app_id = options.get("app_id") or os.environ.get("TRADERA_APP_ID")
    app_key = options.get("app_key") or os.environ.get("TRADERA_APP_KEY")
    return (str(app_id), str(app_key)) if app_id and app_key else None


class TraderaApiSource(Source):
    name = "tradera"
    description = "Tradera REST API v4 (official; needs TRADERA_APP_ID and TRADERA_APP_KEY)"

    def __init__(self, fetcher, options: dict | None = None) -> None:
        super().__init__(fetcher, options)
        pair = credentials(self.options)
        if pair is None:
            raise SourceError(
                "the Tradera API source needs TRADERA_APP_ID and TRADERA_APP_KEY"
            )
        self.app_id, self.app_key = pair

    @property
    def headers(self) -> dict:
        return {
            "X-App-Id": self.app_id,
            "X-App-Key": self.app_key,
            "Accept": "application/json",
        }

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        sort = self.options.get("sort", DEFAULT_SORT)
        if sort not in SORT_ORDERS:
            raise SourceError(
                f"unknown Tradera sort {sort!r}; one of: {', '.join(SORT_ORDERS)}"
            )

        seen: set[str] = set()
        out: list[Listing] = []
        for page in range(1, MAX_PAGES + 1):
            params = {"query": criteria.query, "pageNumber": page, "orderBy": sort}
            if self.options.get("category_id"):
                params["categoryId"] = self.options["category_id"]
            response = self.fetcher.get(SEARCH_URL, params=params, headers=self.headers)
            if response.status_code == 401:
                raise SourceError(
                    "Tradera rejected the credentials (401); check app_id/app_key "
                    "under source_options.tradera"
                )
            response.raise_for_status()
            payload = response.json()

            for error in payload.get("errors") or []:
                log.warning("tradera search error: %s", error)

            fresh = 0
            for item in payload.get("items") or []:
                listing = self._to_listing(item)
                if listing is None or listing.external_id in seen:
                    continue
                seen.add(listing.external_id)
                out.append(listing)
                fresh += 1

            if not fresh or len(out) >= criteria.limit or page >= _last_page(payload):
                break

        return out[: criteria.limit]

    def _to_listing(self, item: dict) -> Listing | None:
        item_id = item.get("id")
        title = (item.get("shortDescription") or "").strip()
        if not item_id or not title:
            return None

        item_type = item.get("itemType") or ""
        is_auction = item_type in _AUCTION_TYPES
        buy_now = _positive(item.get("buyItNowPrice"))
        # `maxBid` is the standing bid on an auction and the shelf price on a
        # ShopItem. Price means "what it costs to own this now", so a buy-now
        # amount wins; a pure auction has only the bid, which `is_auction`
        # marks as provisional for the ranking.
        current_bid = _positive(item.get("maxBid")) if is_auction else None

        return Listing(
            source=self.name,
            external_id=str(item_id),
            title=title,
            url=_web_url(item.get("itemUrl"), item_id),
            price=buy_now if buy_now is not None else _positive(item.get("maxBid")),
            currency="SEK",
            seller=item.get("sellerAlias") or "",
            description=(item.get("longDescription") or "").strip(),
            image_url=_image(item),
            raw={
                "country": "SE",
                "item_type": item_type,
                "is_auction": is_auction,
                "buy_now_price": buy_now,
                "current_bid": current_bid,
                "bid_count": item.get("bidCount") or 0,
                "next_bid": _positive(item.get("nextBid")),
                "ends_at": item.get("endDate") or "",
                "is_ended": bool(item.get("isEnded")),
                "category_id": item.get("categoryId"),
                "seller_id": item.get("sellerId"),
                "attributes": _attributes(item),
            },
        )


def _last_page(payload: dict) -> int:
    total = payload.get("totalNumberOfPages")
    return total if isinstance(total, int) and total > 0 else 1


def _positive(value) -> float | None:
    """Tradera sends 0 for "no such price", which is not a price."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def _web_url(url: str | None, item_id) -> str:
    """The API hands out http:// links; the site is https everywhere."""
    if not url:
        return f"{WEB_BASE}/item/{item_id}"
    return url.split("?")[0].replace("http://", "https://", 1)


def _image(item: dict) -> str:
    """Prefer the gallery-sized image; fall back to the thumbnail."""
    for link in item.get("imageLinks") or []:
        if isinstance(link, dict) and link.get("format") == "gallery" and link.get("url"):
            return link["url"]
    return item.get("thumbnailLink") or ""


def _attributes(item: dict) -> dict:
    """Flatten the seller's structured attributes ("condition", brand, ...)."""
    values = (item.get("attributeValues") or {}).get("termAttributeValues") or []
    out = {}
    for entry in values:
        name = entry.get("name")
        vals = entry.get("values") or []
        if name and vals:
            out[name] = vals[0] if len(vals) == 1 else vals
    return out
