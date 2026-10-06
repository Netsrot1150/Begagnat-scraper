"""Source registry."""

from __future__ import annotations

from .base import Source, SourceError
from .blocket import BlocketSource
from .tradera import TraderaSource
from .tradera_api import TraderaApiSource, credentials as tradera_credentials
from .ebay import EbaySource
from .ebay_api import EbayApiSource, credentials as ebay_credentials
from .marketplace import MarketplaceSource

ALL_SOURCES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (BlocketSource, TraderaSource, EbaySource, MarketplaceSource)
}

#: Sources used when the user does not pass --source. Marketplace is excluded
#: because it needs a logged-in browser.
DEFAULT_SOURCES = ["blocket", "tradera", "ebay"]


#: Sources that have both an official API and an HTML scraper. The API is used
#: whenever the user has registered an application; the scraper is the fallback.
#: Both implementations keep the same `name`, deliberately, so a listing keeps
#: the same key whichever way it was found.
_API_UPGRADES = {
    "ebay": (ebay_credentials, EbayApiSource),
    "tradera": (tradera_credentials, TraderaApiSource),
}


def resolve(name: str, options: dict) -> type[Source]:
    """Pick the implementation for a source name."""
    has_credentials, api_cls = _API_UPGRADES.get(name, (None, None))
    if has_credentials is not None and has_credentials(options) is not None:
        return api_cls
    cls = ALL_SOURCES.get(name)
    if cls is None:
        raise SourceError(f"unknown source {name!r}; available: {', '.join(ALL_SOURCES)}")
    return cls


def build(names: list[str], fetcher, options: dict | None = None) -> list[Source]:
    options = options or {}
    sources = []
    for name in names:
        source_options = options.get(name, {})
        sources.append(resolve(name, source_options)(fetcher, source_options))
    return sources


__all__ = [
    "Source",
    "SourceError",
    "ALL_SOURCES",
    "DEFAULT_SOURCES",
    "build",
    "resolve",
]
