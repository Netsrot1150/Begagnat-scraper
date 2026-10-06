"""Source plugin contract."""

from __future__ import annotations

import logging
from typing import Iterable

from ..http import Fetcher
from ..models import Listing
from ..query import Criteria


class SourceError(RuntimeError):
    """A source could not complete a search. Never fatal for the whole run."""


class Source:
    name: str = "base"
    #: Human-readable note shown by ``begagnat sources``.
    description: str = ""
    #: Set for sources that need credentials, a browser, or manual setup.
    requires_setup: str = ""

    def __init__(self, fetcher: Fetcher, options: dict | None = None) -> None:
        self.fetcher = fetcher
        self.options = options or {}
        self.log = logging.getLogger(f"begagnat.{self.name}")

    def search(self, criteria: Criteria) -> Iterable[Listing]:
        raise NotImplementedError
