"""eBay.

Scrapes the ordinary search results page. The site is available per country;
``--ebay-domain ebay.de`` restricts to a domain whose sellers are inside the
EU, which matters because non-EU purchases pick up 25% VAT plus a clearance
fee (see :mod:`begagnat.fx`).
"""

from __future__ import annotations

import re
from typing import Iterable

from bs4 import BeautifulSoup

from ..models import Listing
from ..parsing import parse_price
from ..query import Criteria
from .base import Source, SourceError

_ITEM_ID_RE = re.compile(r"/itm/(?:[^/]+/)?(\d{9,})")

# Which country each eBay domain's sellers are in, for the import-cost estimate.
DOMAIN_COUNTRY = {
    "ebay.com": "US", "ebay.co.uk": "GB", "ebay.de": "DE", "ebay.fr": "FR",
    "ebay.it": "IT", "ebay.es": "ES", "ebay.nl": "NL", "ebay.ie": "IE",
    "ebay.at": "AT", "ebay.pl": "PL", "ebay.ca": "CA", "ebay.com.au": "AU",
}
_LOCATION_RE = re.compile(r"(?:from|från)\s+([A-Za-zÅÄÖåäö .]+)", re.I)


class EbaySource(Source):
    name = "ebay"
    description = "eBay search results (set --ebay-domain to pick a marketplace)"

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        domain = self.options.get("domain", "ebay.com")
        params = {
            "_nkw": criteria.query,
            "_ipg": min(criteria.limit, 240),
            "_sop": self.options.get("sort", "10"),   # 10 = newly listed
            "LH_PrefLoc": self.options.get("pref_loc", "2"),  # 2 = worldwide
        }
        if self.options.get("buy_it_now"):
            params["LH_BIN"] = "1"
        if self.options.get("used_only", True):
            params["LH_ItemCondition"] = "4|10|1000|2500|3000"  # used / refurbished

        response = self.fetcher.get(f"https://www.{domain}/sch/i.html", params=params)
        if response.status_code == 403:
            # eBay blocks plain HTTP clients from whole networks, and it does so
            # for the homepage too - there is nothing to fix in the parser.
            raise SourceError(
                "eBay returned 403 for this network. Use the official Browse API "
                "instead: register a free app at https://developer.ebay.com/ and set "
                "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET; the API source is picked up "
                "automatically when both are present."
            )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        seen: set[str] = set()
        # eBay has migrated card markup more than once; accept every generation.
        cards = soup.select("li.s-item, li.s-card, div.s-item, li[data-viewport]")
        for card in cards:
            listing = self._from_card(card, domain)
            if listing and listing.external_id not in seen:
                seen.add(listing.external_id)
                yield listing

    def _from_card(self, card, domain: str) -> Listing | None:
        anchor = card.select_one('a[href*="/itm/"]')
        if not anchor:
            return None
        match = _ITEM_ID_RE.search(anchor.get("href", ""))
        if not match:
            return None

        title_el = card.select_one(
            ".s-item__title, .s-card__title, .su-styled-text.primary, h3"
        )
        title = (title_el.get_text(" ", strip=True) if title_el else anchor.get_text(" ", strip=True))
        title = re.sub(r"^(new listing|nytt objekt)\s*", "", title, flags=re.I).strip()
        if not title or title.lower() == "shop on ebay":
            return None

        price_el = card.select_one(".s-item__price, .s-card__price, .s-item__detail--primary")
        price, currency = parse_price(price_el.get_text(" ", strip=True) if price_el else "")

        shipping = None
        ship_el = card.select_one(".s-item__shipping, .s-item__logisticsCost, .s-card__shipping")
        if ship_el:
            ship_text = ship_el.get_text(" ", strip=True)
            if "free" not in ship_text.lower() and "fri" not in ship_text.lower():
                shipping, _ = parse_price(ship_text)  # same currency as the item price

        location = ""
        loc_el = card.select_one(".s-item__location, .s-item__itemLocation, .s-card__location")
        if loc_el:
            location_text = loc_el.get_text(" ", strip=True)
            location_match = _LOCATION_RE.search(location_text)
            location = (location_match.group(1) if location_match else location_text).strip()

        image_el = card.select_one("img")
        image = (image_el.get("src") or image_el.get("data-src") or "") if image_el else ""

        return Listing(
            source=self.name,
            external_id=match.group(1),
            title=title,
            url=anchor["href"].split("?")[0],
            price=price,
            currency=currency,
            location=location or domain,
            shipping=shipping,
            image_url=image,
            raw={"country": _country_for(location, domain), "domain": domain},
        )


def _country_for(location: str, domain: str) -> str:
    """Guess the seller's country: the card's own text wins over the domain."""
    low = (location or "").lower()
    named = {
        "sweden": "SE", "sverige": "SE", "germany": "DE", "deutschland": "DE",
        "netherlands": "NL", "france": "FR", "italy": "IT", "spain": "ES",
        "poland": "PL", "ireland": "IE", "austria": "AT", "denmark": "DK",
        "finland": "FI", "united kingdom": "GB", "china": "CN", "hong kong": "HK",
        "united states": "US", "japan": "JP", "canada": "CA",
    }
    for needle, iso in named.items():
        if needle in low:
            return iso
    return DOMAIN_COUNTRY.get(domain, "")
