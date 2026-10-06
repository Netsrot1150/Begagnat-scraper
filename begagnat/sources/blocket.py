"""Blocket.

Blocket has moved its search backend more than once, so this source tries the
known endpoints in order of how likely they are to work today:

1. The ``recommerce`` search API the site currently calls. No authentication -
   a plain User-Agent is enough. (Endpoint confirmed by the community-run
   ``dunderrrrrr/blocket_api`` package.)
2. The older ``search_bff`` API, which needs a bearer token that the site
   itself hands out without a login.
3. The rendered search page, reading the ``__NEXT_DATA__`` payload.

All three return ad objects with slightly different key names, so a single
tolerant extractor handles the shape differences rather than three parsers.
"""

from __future__ import annotations

from typing import Iterable

from bs4 import BeautifulSoup

from ..models import Listing
from ..parsing import find_price, json_scripts, parse_price, parse_timestamp, walk
from ..query import Criteria
from . import blocket_regions
from .base import Source, SourceError

RECOMMERCE_URL = (
    "https://www.blocket.se/recommerce/forsale/search/api/search/SEARCH_ID_BAP_COMMON"
)
TOKEN_URL = "https://www.blocket.se/api/adout-api-route/refresh-token-and-validate-session"
BFF_URL = "https://api.blocket.se/search_bff/v2/content"
HTML_SEARCH_URL = "https://www.blocket.se/annonser/hela_sverige"

_ID_KEYS = ("ad_id", "adId", "itemId", "id")
_TITLE_KEYS = ("subject", "title", "heading", "name")
_PRICE_KEYS = ("price", "list_price", "listPrice", "amount")
_TIME_KEYS = ("list_time", "listTime", "published", "timestamp")

#: Blocket serves a fixed page of ~53 ads and ignores a page-size parameter, so
#: `limit` has to be spent on pages. Measured live 2026-09-04: "grafikkort"
#: returns 1656 ads over 32 pages, and reading only the first page saw 3% of
#: the market. The cap is a courtesy to the site, at 1.5 s between requests.
MAX_PAGES = 12
#: Newest first. The site's own relevance ranking adds nothing here - the tool
#: does its own scoring - and a watch that only ever reads page one has to see
#: the ads that appeared since it last ran.
DEFAULT_SORT = "PUBLISHED_DESC"


class BlocketSource(Source):
    name = "blocket"
    description = "Blocket.se - Sweden's largest classifieds site (JSON API, no login)"

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        # A misspelt region is the user's mistake, not a dead endpoint, so it
        # must not be swallowed by the fallback chain below.
        if criteria.region and blocket_regions.code_for(criteria.region) is None:
            raise SourceError(
                f"unknown Blocket region {criteria.region!r}; "
                f"try one of: {', '.join(blocket_regions.names())}"
            )

        strategies = [
            ("recommerce API", self._search_recommerce),
            ("search_bff API", self._search_bff),
            ("HTML search page", self._search_html),
        ]
        errors = []
        for label, strategy in strategies:
            try:
                listings = list(strategy(criteria))
            except Exception as exc:  # noqa: BLE001 - fall through to the next strategy
                self.log.warning("Blocket %s failed: %s", label, exc)
                errors.append(f"{label} ({type(exc).__name__})")
                continue
            if listings:
                self.log.info("Blocket %s returned %d listings", label, len(listings))
                return listings
            self.log.info("Blocket %s returned nothing, trying the next strategy", label)
        # The full exceptions are on the log at -v; keep the summary readable.
        detail = ", ".join(errors) if errors else "all strategies returned nothing"
        raise SourceError(f"no Blocket strategy worked: {detail}")

    # ------------------------------------------------------------ strategies
    def _search_recommerce(self, criteria: Criteria) -> Iterable[Listing]:
        location = blocket_regions.code_for(criteria.region)
        listings: list[Listing] = []
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            params = {"q": criteria.query, "page": page, "sort": DEFAULT_SORT}
            if location:
                params["location"] = location
            response = self.fetcher.get(
                RECOMMERCE_URL, params=params, headers={"Referer": "https://www.blocket.se/"}
            )
            response.raise_for_status()
            payload = response.json()

            fresh = [l for l in self._extract(payload) if l.external_id not in seen]
            seen.update(l.external_id for l in fresh)
            listings.extend(fresh)

            if not fresh or len(listings) >= criteria.limit or page >= _last_page(payload):
                break

        return listings[: criteria.limit]

    def _search_bff(self, criteria: Criteria) -> Iterable[Listing]:
        token_response = self.fetcher.get(TOKEN_URL, headers={"Referer": "https://www.blocket.se/"})
        token_response.raise_for_status()
        payload = token_response.json()
        token = payload.get("bearerToken") or payload.get("token") or payload.get("access_token")
        if not token:
            raise SourceError("no bearer token in Blocket token response")

        params = {"q": criteria.query, "lim": min(criteria.limit, 99), "sort": "date", "status": "active"}
        if criteria.region:
            params["r"] = criteria.region
        response = self.fetcher.get(
            BFF_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Referer": "https://www.blocket.se/"},
        )
        response.raise_for_status()
        return self._extract(response.json())

    def _search_html(self, criteria: Criteria) -> Iterable[Listing]:
        response = self.fetcher.get(HTML_SEARCH_URL, params={"q": criteria.query})
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        listings = []
        for blob in json_scripts(soup, script_id="__NEXT_DATA__"):
            listings.extend(self._extract(blob))
        if listings:
            return listings

        # Last resort: the rendered cards.
        seen: set[str] = set()
        for anchor in soup.select('a[href*="/annons/"]'):
            href = anchor.get("href", "")
            ad_id = href.rstrip("/").split("-")[-1]
            title = anchor.get_text(" ", strip=True)
            if not ad_id.isdigit() or not title or ad_id in seen:
                continue
            seen.add(ad_id)
            card = anchor.find_parent(["article", "div"]) or anchor
            price, _ = find_price(card.get_text(" ", strip=True))
            listings.append(
                Listing(
                    source=self.name, external_id=ad_id, title=title,
                    url=href if href.startswith("http") else f"https://www.blocket.se{href}",
                    price=price, currency="SEK", raw={"country": "SE"},
                )
            )
        return listings

    # -------------------------------------------------------------- extraction
    def _extract(self, payload) -> list[Listing]:
        """Pull ad-shaped objects out of any of the response formats.

        Rather than hard-coding one response envelope per endpoint, walk the
        JSON and keep every node that has an id, a title and a price. The
        endpoints agree on the content; they disagree on where it is nested.
        """
        listings: list[Listing] = []
        seen: set[str] = set()
        for node in walk(payload):
            ad_id = _first_str(node, _ID_KEYS)
            title = _first_str(node, _TITLE_KEYS)
            if not ad_id or not title or ad_id in seen:
                continue
            if not any(key in node for key in _PRICE_KEYS):
                continue
            seen.add(ad_id)
            listings.append(self._from_ad(node, ad_id, title))
        return listings

    def _from_ad(self, ad: dict, ad_id: str, title: str) -> Listing:
        price = None
        for key in _PRICE_KEYS:
            price = _amount(ad.get(key))
            if price is not None:
                break

        location = ""
        places = ad.get("location") or ad.get("locations") or []
        if isinstance(places, list):
            location = ", ".join(
                p.get("name", "") for p in places if isinstance(p, dict) and p.get("name")
            )
        elif isinstance(places, str):
            location = places
        elif isinstance(places, dict):
            location = places.get("name", "")

        images = ad.get("images") or ad.get("image") or []
        image = ""
        if isinstance(images, list) and images:
            first = images[0]
            image = first.get("url", "") if isinstance(first, dict) else str(first)
        elif isinstance(images, dict):
            image = images.get("url", "")

        advertiser = ad.get("advertiser") or ad.get("seller") or {}
        seller = advertiser.get("name", "") if isinstance(advertiser, dict) else str(advertiser)

        url = ad.get("share_url") or ad.get("url") or ad.get("canonical_url") or ""
        if url and not url.startswith("http"):
            url = f"https://www.blocket.se{url}"

        return Listing(
            source=self.name,
            external_id=ad_id,
            title=title.strip(),
            url=url or f"https://www.blocket.se/annons/{ad_id}",
            price=price,
            currency="SEK",
            location=location,
            seller=seller,
            posted_at=parse_timestamp(next((ad[k] for k in _TIME_KEYS if ad.get(k)), None)),
            description=str(ad.get("body") or ad.get("description") or "")[:2000],
            image_url=image,
            raw={"country": "SE"},
        )


def _last_page(payload) -> int:
    """How many pages the search has, per the response's own metadata.

    The live endpoint reports ``metadata.paging.last``. Anything else - an
    older envelope, a stubbed response - is treated as a single page, so a
    shape change costs us the extra pages rather than an endless loop.
    """
    if not isinstance(payload, dict):
        return 1
    paging = (payload.get("metadata") or {}).get("paging") or {}
    last = paging.get("last")
    return last if isinstance(last, int) and last > 0 else 1


def _first_str(node: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _amount(value) -> float | None:
    """Prices arrive as a number, a {'value': n} object, or a '1 250 kr' label."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("value", "amount", "price"):
            nested = _amount(value.get(key))
            if nested is not None:
                return nested
        return parse_price(value.get("label") or value.get("suffix"))[0]
    if isinstance(value, str):
        return parse_price(value)[0]
    return None
