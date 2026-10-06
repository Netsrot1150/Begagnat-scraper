"""eBay Browse API - the official, permitted way in.

eBay serves `403` to plain HTTP clients from most networks, so the HTML source
in `ebay.py` fails outright for many users. The Browse API is free, documented
and returns structured condition, location and shipping - all three of which
the HTML scraper had to guess at.

Setup, once:

1. Register an application at https://developer.ebay.com/ (free).
2. Copy the *production* App ID (client id) and Cert ID (client secret).
3. Export them:  EBAY_CLIENT_ID=... EBAY_CLIENT_SECRET=...

`EbayApiSource` is used automatically whenever those two are present; without
them the run falls back to `EbaySource` and its HTML scraping.

Field names here come from eBay's published OpenAPI contract for
buy/browse/v1. Verified live on 2026-09-05 against production credentials:
the client-credentials token, the item_summary search, EUR and USD conversion,
shipping cost and the import-VAT path all behave as written, on ebay.de and
ebay.com. What the first live run did *not* survive was the accessory filter -
see ACCESSORY_HINTS.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from typing import Iterable

from ..models import Listing
from ..query import Criteria
from .base import Source, SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
PUBLIC_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Which marketplace to search, and where that marketplace's sellers mostly are.
# The country only seeds the import-cost estimate; a per-item `itemLocation`
# overrides it whenever eBay sends one.
MARKETPLACES = {
    "ebay.com": ("EBAY_US", "US"),
    "ebay.co.uk": ("EBAY_GB", "GB"),
    "ebay.de": ("EBAY_DE", "DE"),
    "ebay.fr": ("EBAY_FR", "FR"),
    "ebay.it": ("EBAY_IT", "IT"),
    "ebay.es": ("EBAY_ES", "ES"),
    "ebay.nl": ("EBAY_NL", "NL"),
    "ebay.ie": ("EBAY_IE", "IE"),
    "ebay.at": ("EBAY_AT", "AT"),
    "ebay.pl": ("EBAY_PL", "PL"),
    "ebay.ca": ("EBAY_CA", "CA"),
    "ebay.com.au": ("EBAY_AU", "AU"),
}

# eBay's `conditions` filter. Anything but NEW, because a new datacentre card is
# never the bargain this tool is looking for.
USED_CONDITIONS = "USED|CERTIFIED_REFURBISHED|SELLER_REFURBISHED|FOR_PARTS_OR_NOT_WORKING"

# Refresh a bit before eBay's own expiry so a long fan-out cannot straddle it.
TOKEN_MARGIN_S = 60


def credentials(options: dict | None = None) -> tuple[str, str] | None:
    """Client id and secret from the source options, else from the environment."""
    options = options or {}
    client_id = options.get("client_id") or os.environ.get("EBAY_CLIENT_ID")
    secret = options.get("client_secret") or os.environ.get("EBAY_CLIENT_SECRET")
    return (client_id, secret) if client_id and secret else None


class EbayApiSource(Source):
    name = "ebay"
    description = "eBay Browse API (official; needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)"

    def __init__(self, fetcher, options: dict | None = None) -> None:
        super().__init__(fetcher, options)
        pair = credentials(self.options)
        if pair is None:
            raise SourceError(
                "the eBay API source needs EBAY_CLIENT_ID and EBAY_CLIENT_SECRET"
            )
        self.client_id, self.client_secret = pair
        self._token = ""
        self._token_expires_at = 0.0

    # ------------------------------------------------------------------ auth

    def token(self) -> str:
        """A client-credentials token, cached until shortly before it expires."""
        if self._token and time.time() < self._token_expires_at:
            return self._token

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        response = self.fetcher.request(
            "POST",
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials", "scope": PUBLIC_SCOPE},
        )
        if response.status_code != 200:
            raise SourceError(
                f"eBay rejected the credentials ({response.status_code}); "
                "check EBAY_CLIENT_ID/EBAY_CLIENT_SECRET are the production pair"
            )
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 7200)) - TOKEN_MARGIN_S
        return self._token

    # ---------------------------------------------------------------- search

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        domain = self.options.get("domain", "ebay.com")
        marketplace, default_country = MARKETPLACES.get(domain, ("EBAY_US", "US"))

        filters = []
        if self.options.get("used_only", True):
            filters.append(f"conditions:{{{USED_CONDITIONS}}}")
        if self.options.get("buy_it_now"):
            filters.append("buyingOptions:{FIXED_PRICE}")
        if criteria.region:
            filters.append(f"itemLocationCountry:{criteria.region}")

        params = {
            "q": criteria.query,
            "limit": min(criteria.limit, 200),
            "sort": self.options.get("sort", "newlyListed"),
        }
        if filters:
            params["filter"] = ",".join(filters)

        response = self.fetcher.get(
            SEARCH_URL,
            params=params,
            headers={
                "Authorization": f"Bearer {self.token()}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
                "Accept": "application/json",
            },
        )
        response.raise_for_status()
        payload = response.json()

        for item in payload.get("itemSummaries") or []:
            listing = self._to_listing(item, domain, default_country)
            if listing is not None:
                yield listing

    def _to_listing(self, item: dict, domain: str, default_country: str) -> Listing | None:
        item_id = item.get("legacyItemId") or item.get("itemId")
        title = (item.get("title") or "").strip()
        if not item_id or not title:
            return None

        buying_options = item.get("buyingOptions") or []
        price, currency = _amount(item.get("price"))
        # A live auction reports `currentBidPrice` and no `price`.
        if price is None:
            price, currency = _amount(item.get("currentBidPrice"))
        current_bid, _ = _amount(item.get("currentBidPrice"))
        # An auction that also offers Buy It Now reports both; `price` is then
        # the fixed price, which is a cost rather than a floor.
        buy_now = price if "FIXED_PRICE" in buying_options else None

        shipping = None
        options = item.get("shippingOptions") or []
        if options:
            shipping, _ = _amount(options[0].get("shippingCost"))

        location = item.get("itemLocation") or {}
        country = (location.get("country") or default_country or "").upper()
        where = ", ".join(
            part for part in (location.get("city"), location.get("stateOrProvince"), country)
            if part
        )

        return Listing(
            source=self.name,
            external_id=str(item_id),
            title=title,
            url=(item.get("itemWebUrl") or "").split("?")[0],
            price=price,
            currency=currency,
            location=where,
            seller=(item.get("seller") or {}).get("username", ""),
            shipping=shipping,
            image_url=(item.get("image") or {}).get("imageUrl", ""),
            raw={
                "country": country,
                "domain": domain,
                "condition": item.get("condition", ""),
                "buying_options": buying_options,
                "is_auction": "AUCTION" in buying_options,
                "current_bid": current_bid,
                "buy_now_price": buy_now,
                "bid_count": item.get("bidCount") or 0,
                "ends_at": item.get("itemEndDate") or "",
            },
        )


def _amount(value) -> tuple[float | None, str]:
    """Read eBay's ConvertedAmount ({"value": "129.99", "currency": "USD"})."""
    if not isinstance(value, dict):
        return None, "USD"
    currency = value.get("currency") or "USD"
    try:
        return float(value["value"]), currency
    except (KeyError, TypeError, ValueError):
        return None, currency
