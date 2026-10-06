"""Facebook Marketplace.

Marketplace has no public API, and plain HTTP is refused below the
application layer (HTTP 400, no cookies), so this source drives a real
headless Chromium through Playwright.

Logged out, a Swedish search still works (verified live 2026-09-21): it
returns one fixed page of about 24 results and scrolling adds nothing. Logged
in, results lazy-load on scroll. An optional saved session is used when it
exists:

    pip install 'begagnat-scraper[browser]' && playwright install chromium
    begagnat login-marketplace          # optional: log in once for more results

Where Facebook demands a login, or in the EU shows its DMA Marketplace consent
screen (``/privacy/consent/?flow=fb_dma_marketplace``), this source reports it
and stops. It never fills in a login form and never answers the consent
screen - both belong to the account owner.

The city is a URL slug. ``stockholm`` works as a word; smaller towns need
Facebook's numeric location id - Sundsvall is ``110103992352737``. An unknown
slug is silently redirected to a location-less search (the browser's
geolocation, which from this network landed in California), so the final URL
is checked and that case is reported too.

Parsing reads each result tile's ``aria-label``, which Facebook renders as one
comma-separated string, captured live in the Swedish UI:
``"Interiörmattor Tesla model 3, 400 kr, Vällingby, AB, säljinlägg 2837..."``.
Class names are obfuscated and rotate; the aria-label is what screen readers
depend on, so it is the most stable thing on the page. When it is missing we
fall back to the tile's visible text lines.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urlencode

from ..models import Listing
from ..parsing import parse_price as _parse_amount
from ..query import Criteria
from .base import Source, SourceError

DEFAULT_PROFILE = Path.home() / ".config" / "begagnat" / "fb-profile"
DEFAULT_CITY = "stockholm"
#: Facebook caps the radius at 500 km.
MAX_RADIUS_KM = 500
_ITEM_RE = re.compile(r"/marketplace/item/(\d+)")

# The price token follows the UI language and the seller's currency: the
# Swedish UI renders "1 400 kr" or "15 000 US$" (no-break spaces), the English
# UI "SEK1,400". The English form carries a comma, which is also the
# aria-label's field separator, so it has to be matched as one token or every
# field after it shifts. "Gratis"/"Free" are real listings with no price.
_CUR = r"(?:kr|sek|us\$|\$|€|£|eur|usd|gbp)"
_PRICE_TOKEN = (
    rf"[0-9][0-9\s .']*\s*{_CUR}"
    rf"|{_CUR}\s?[0-9][0-9,.\s ]*"
    r"|gratis|free"
)
_ARIA_RE = re.compile(
    rf"^(?P<title>.*?),\s*(?P<price>{_PRICE_TOKEN}),\s*"
    # A price cut adds "nedsatt från 6 500 kr" (English: "reduced from") before the place.
    rf"(?:(?:nedsatt från|reduced from)\s*(?P<was>{_PRICE_TOKEN}),\s*)?"
    r"(?P<place>.+?),\s*[^,\d]*(?P<id>\d+)\s*$",
    re.I | re.S,
)
# Badges Facebook prints on a tile above the title; never a title themselves.
_BADGES = {"nyligen publicerat", "just listed", "sänkt pris", "price dropped"}


def build_search_url(query: str, city: str = DEFAULT_CITY, radius_km: int = MAX_RADIUS_KM,
                     sort: str = "creation_time_descend") -> str:
    """The search URL. Newest first, so a watch sees fresh ads on page one."""
    params = {
        "query": query,
        "exact": "false",
        "radius": min(int(radius_km), MAX_RADIUS_KM),
        "sortBy": sort,
    }
    return f"https://www.facebook.com/marketplace/{city}/search?{urlencode(params)}"


def blocked_reason(url: str) -> str | None:
    """Why the browser did not land on the search page, or None if it did.

    Checked against the final URL after navigation rather than inferred from
    zero results, so "logged out" is never mistaken for "no matching ads".
    """
    if "/login" in url or "/checkpoint" in url or "two_step_verification" in url:
        return ("Facebook sent the browser to its login page. Run 'begagnat login-marketplace' "
                "and log in by hand; the session is then reused.")
    if "/privacy/consent" in url:
        return ("Facebook sent the browser to its Marketplace consent screen (EU DMA). Run "
                "'begagnat login-marketplace' and answer it yourself; this tool will not make "
                "that privacy choice for you.")
    if "/marketplace/category/search" in url:
        return ("Facebook did not recognise the city and fell back to a location-less search. "
                "Use its numeric location id as the city (Sundsvall: 110103992352737).")
    return None


def parse_price(token: str | None) -> tuple[float | None, str]:
    """``(amount, currency)`` from a price token; amount None when free or unreadable."""
    if not token or token.strip().lower() in ("gratis", "free"):
        return None, "SEK"
    amount, currency = _parse_amount(token)
    return (amount or None), currency


class MarketplaceSource(Source):
    name = "marketplace"
    description = "Facebook Marketplace (Playwright; login optional)"
    requires_setup = (
        "pip install 'begagnat-scraper[browser]' and 'playwright install chromium'. "
        "Optional: 'begagnat login-marketplace' once for more than ~24 results."
    )

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        profile = Path(self.options.get("profile", DEFAULT_PROFILE))
        url = build_search_url(
            criteria.query,
            city=str(self.options.get("city") or DEFAULT_CITY),
            radius_km=int(self.options.get("radius_km", MAX_RADIUS_KM)),
        )
        html = self._render(url, profile if profile.exists() else None)
        return list(self._parse(html))[: criteria.limit]

    def _render(self, url: str, profile: Path | None) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise SourceError(
                "playwright is not installed; pip install 'begagnat-scraper[browser]' "
                "and run 'playwright install chromium'"
            ) from exc

        with tempfile.TemporaryDirectory() as scratch, sync_playwright() as pw:
            # No saved session: a throwaway profile, so nothing persists between runs.
            context = pw.chromium.launch_persistent_context(
                str(profile or scratch), headless=self.options.get("headless", True),
                locale="sv-SE",
            )
            try:
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(3_000)
                reason = blocked_reason(page.url)
                if reason:
                    raise SourceError(reason)
                # Logged in, results lazy-load on scroll; stop once the page stops growing.
                last = 0
                for _ in range(int(self.options.get("scrolls", 4))):
                    page.mouse.wheel(0, 4_000)
                    page.wait_for_timeout(1_500)
                    height = page.evaluate("document.body.scrollHeight")
                    if height == last:
                        break
                    last = height
                return page.content()
            finally:
                context.close()

    def _parse(self, html: str) -> Iterable[Listing]:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        for anchor in soup.select('a[href*="/marketplace/item/"]'):
            match = _ITEM_RE.search(anchor.get("href", ""))
            if not match or match.group(1) in seen:
                continue
            item_id = match.group(1)
            seen.add(item_id)

            label = anchor.get("aria-label") or ""
            aria = _ARIA_RE.match(label.strip())
            if aria:
                # A matched label with an empty title is a listing without one
                # (the tile shows only price and place) - nothing to search on.
                parsed = _from_aria(aria)
            else:
                parsed = _from_text(anchor)
            if not parsed:
                continue
            title, (price, currency), location, was = parsed
            img = anchor.find("img")
            raw = {"country": "SE"}
            if was:
                raw["previous_price"] = was
            yield Listing(
                source=self.name,
                external_id=item_id,
                title=title,
                url=f"https://www.facebook.com/marketplace/item/{item_id}",
                price=price,
                currency=currency,
                location=location,
                image_url=(img.get("src") or "") if img else "",
                raw=raw,
            )


def _from_aria(match):
    title = " ".join(match.group("title").split())
    if not title:
        return None
    was = parse_price(match.group("was"))[0] if match.group("was") else None
    return title, parse_price(match.group("price")), match.group("place").strip(), was


def _from_text(anchor):
    """Fallback: card text comes out as "1 400 kr\\nTesla P100 16GB\\nSundsvall"."""
    lines = [line.strip() for line in anchor.get_text("\n", strip=True).split("\n") if line.strip()]
    price: tuple[float | None, str] = (None, "SEK")
    rest: list[str] = []
    for line in lines:
        if line.lower() in _BADGES:
            continue
        if re.fullmatch(_PRICE_TOKEN, line, re.I):
            if price[0] is None:
                price = parse_price(line)  # the first price is the current one
        else:
            rest.append(line)
    if not rest:
        return None
    title = max(rest, key=len)
    location = rest[-1] if len(rest) > 1 and rest[-1] != title else ""
    return title, price, location, None
