"""Tradera.

Tradera's search results are server-rendered, so we read the page directly.
Four extraction strategies are tried in order of reliability: the item-card
markup (verified against a live capture on 2026-09-04), the Next.js payload,
JSON-LD product data, then a generic anchor sweep. The chain is the design -
Tradera has already dropped __NEXT_DATA__ once, so keep the old strategies
behind the new one instead of replacing them.
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Listing
from ..parsing import find_price, json_scripts, parse_price, parse_timestamp, walk
from ..query import Criteria
from .base import Source

BASE = "https://www.tradera.com"
SEARCH_URL = f"{BASE}/search"
_ITEM_ID_RE = re.compile(r"/item/(?:\d+/)?(\d+)")
_CARD_ID_RE = re.compile(r"^item-card-(\d+)$")
# Auctions and auctions-with-a-buy-now: the visible price is a bid, not a cost.
_AUCTION_TYPES = {"Auction", "AuctionBin"}


class TraderaSource(Source):
    name = "tradera"
    description = "Tradera.com - Swedish auction site (HTML scraping, no login)"

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        params = {"q": criteria.query, "sortBy": "AddedOn", "itemsPerPage": min(criteria.limit, 60)}
        if criteria.region:
            params["countyIds"] = criteria.region
        response = self.fetcher.get(SEARCH_URL, params=params)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        listings = (
            self._from_item_cards(soup)
            or self._from_next_data(soup)
            or self._from_json_ld(soup)
            or self._from_cards(soup)
        )
        seen: set[str] = set()
        for listing in listings:
            if listing.external_id in seen:
                continue
            seen.add(listing.external_id)
            yield listing

    def _from_item_cards(self, soup) -> list[Listing]:
        """Read the rendered item cards - the shape Tradera actually ships.

        Class names are build-hashed and useless, but every card is anchored by
        ``id="item-card-<id>"`` and ``data-item-type``, and the money sits in
        stable ``data-testid`` spans. Price means "what it costs to own this
        now": the buy-now amount where there is one, the current bid otherwise.
        """
        out: list[Listing] = []
        for card in soup.select("div[data-item-type]"):
            match = _CARD_ID_RE.match(card.get("id") or "")
            if not match:
                continue
            item_id = match.group(1)
            anchor = card.select_one('a[href*="/item/"][title]') or card.select_one('a[href*="/item/"]')
            if anchor is None:
                continue
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            if not title:
                continue

            item_type = card.get("data-item-type") or ""
            price_block = card.find(id=f"item-card-{item_id}-price")
            shown, currency = _card_price(price_block, "price")
            buy_now, _ = _card_price(price_block, "bin-price")
            if buy_now is None and item_type in ("PureBin", "ShopItem"):
                buy_now = shown

            time_block = card.find(id=f"item-card-{item_id}-time")
            ends_at = ""
            if time_block is not None:
                visible = time_block.select_one('span[aria-hidden="false"]')
                ends_at = (visible or time_block).get_text(" ", strip=True)

            image = card.select_one("img[src]")
            out.append(
                Listing(
                    source=self.name,
                    external_id=item_id,
                    title=title,
                    url=urljoin(BASE, anchor.get("href", "")),
                    price=buy_now if buy_now is not None else shown,
                    currency=currency,
                    image_url=(image.get("src") if image else "") or "",
                    raw={
                        "country": "SE",
                        "item_type": item_type,
                        "is_auction": item_type in _AUCTION_TYPES,
                        "buy_now_price": buy_now,
                        "current_bid": shown if item_type in _AUCTION_TYPES else None,
                        "ends_at": ends_at,
                    },
                )
            )
        return out

    def _from_next_data(self, soup) -> list[Listing]:
        out: list[Listing] = []
        for blob in json_scripts(soup, script_id="__NEXT_DATA__"):
            for node in walk(blob):
                item_id = node.get("itemId") or node.get("id")
                title = node.get("shortDescription") or node.get("title")
                if not item_id or not isinstance(title, str) or not title.strip():
                    continue
                if not any(k in node for k in ("price", "buyItNowPrice", "maxBidPrice", "currentBidPrice")):
                    continue
                price = _first_number(
                    node.get("buyItNowPrice"), node.get("maxBidPrice"),
                    node.get("currentBidPrice"), node.get("price"),
                )
                url = node.get("itemUrl") or node.get("url") or f"/item/{item_id}"
                out.append(
                    Listing(
                        source=self.name,
                        external_id=str(item_id),
                        title=title.strip(),
                        url=urljoin(BASE, url),
                        price=price,
                        currency="SEK",
                        location=node.get("sellerLocation") or node.get("location") or "",
                        seller=(node.get("sellerAlias") or node.get("seller") or ""),
                        posted_at=parse_timestamp(node.get("startDate") or node.get("addedOn")),
                        image_url=node.get("imageUrl") or "",
                        raw={"country": "SE", "ends_at": node.get("endDate")},
                    )
                )
        return out

    def _from_json_ld(self, soup) -> list[Listing]:
        out: list[Listing] = []
        for blob in json_scripts(soup, script_type="application/ld+json"):
            for node in walk(blob):
                if node.get("@type") not in ("Product", "Offer", "ListItem"):
                    continue
                item = node.get("item") if node.get("@type") == "ListItem" else node
                if not isinstance(item, dict):
                    continue
                url = item.get("url") or ""
                match = _ITEM_ID_RE.search(url)
                if not match or not item.get("name"):
                    continue
                offers = item.get("offers") or {}
                price = offers.get("price") if isinstance(offers, dict) else None
                out.append(
                    Listing(
                        source=self.name,
                        external_id=match.group(1),
                        title=str(item["name"]).strip(),
                        url=urljoin(BASE, url),
                        price=float(price) if price not in (None, "") else None,
                        currency=(offers.get("priceCurrency") if isinstance(offers, dict) else None) or "SEK",
                        image_url=item.get("image") or "",
                        raw={"country": "SE"},
                    )
                )
        return out

    def _from_cards(self, soup) -> list[Listing]:
        out: list[Listing] = []
        for anchor in soup.select('a[href*="/item/"]'):
            href = anchor.get("href", "")
            match = _ITEM_ID_RE.search(href)
            if not match:
                continue
            card = anchor.find_parent(["article", "li", "div"]) or anchor
            title = (
                anchor.get("title")
                or anchor.get("aria-label")
                or anchor.get_text(" ", strip=True)
            ).strip()
            if not title:
                continue
            price, currency = find_price(card.get_text(" ", strip=True))
            out.append(
                Listing(
                    source=self.name,
                    external_id=match.group(1),
                    title=title,
                    url=urljoin(BASE, href),
                    price=price,
                    currency=currency,
                    raw={"country": "SE"},
                )
            )
        return out


def _card_price(block, testid: str) -> tuple[float | None, str]:
    """Read one ``data-testid`` money span out of a card's price block."""
    if block is None:
        return None, "SEK"
    span = block.select_one(f'[data-testid="{testid}"]')
    if span is None:
        return None, "SEK"
    return find_price(span.get_text(" ", strip=True))


def _first_number(*values) -> float | None:
    for value in values:
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        if isinstance(value, dict):
            nested = _first_number(value.get("amount"), value.get("value"))
            if nested:
                return nested
        if isinstance(value, str):
            parsed, _ = parse_price(value)
            if parsed:
                return parsed
    return None
