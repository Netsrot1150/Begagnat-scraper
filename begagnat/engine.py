"""Runs a search across sources, analyses the results, and records them."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .fx import Fx
from .models import Listing
from .query import Criteria, analyse, score, sort_key
from .sources import Source, build
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    hits: list[Listing] = field(default_factory=list)      # passed every filter
    rejected: list[Listing] = field(default_factory=list)  # seen but filtered out
    new: list[Listing] = field(default_factory=list)       # hits not seen before
    price_drops: list[tuple[Listing, float]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)   # source -> error text
    fetched: int = 0
    ai_summary: str = ""


def run_search(
    criteria: Criteria,
    sources: list[Source],
    fx: Fx,
    store: Store | None = None,
    *,
    parallel: bool = True,
    validator=None,
    rescue: bool = False,
) -> RunResult:
    """Search every source, analyse each listing, and diff against the store.

    A source that fails is recorded in ``errors`` and the run continues - one
    marketplace changing its markup must not cost you the other three.

    When a ``validator`` is given, Claude reviews the surviving listings (and,
    with ``rescue``, the ones dropped only for an unrecognised model) before
    anything is recorded or reported.
    """
    result = RunResult()

    def fetch(source: Source) -> tuple[str, list[Listing] | Exception]:
        try:
            return source.name, list(source.search(criteria))
        except Exception as exc:  # noqa: BLE001 - isolate source failures
            log.warning("source %s failed: %s", source.name, exc)
            return source.name, exc

    if parallel and len(sources) > 1:
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:
            outcomes = list(pool.map(fetch, sources))
    else:
        outcomes = [fetch(source) for source in sources]

    for name, outcome in outcomes:
        if isinstance(outcome, Exception):
            result.errors[name] = str(outcome)
            continue
        result.fetched += len(outcome)
        for listing in outcome:
            analyse(listing, criteria, fx)
            if listing.rejected_reason:
                result.rejected.append(listing)
            else:
                result.hits.append(listing)

    if validator is not None:
        _validate(result, criteria, fx, validator, rescue)

    result.hits.sort(key=sort_key)

    if store:
        for listing in result.hits:
            is_new, previous_price = store.record(listing)
            if is_new:
                result.new.append(listing)
            elif (
                previous_price
                and listing.price_sek
                and listing.price_sek < previous_price * 0.95
                # A bid only falls when the auction was restarted, which is not
                # the seller dropping the price.
                and not listing.price_is_provisional
            ):
                result.price_drops.append((listing, previous_price))

    return result


def _validate(result: RunResult, criteria: Criteria, fx: Fx, validator, rescue: bool) -> None:
    """Run the AI validator over the run and fold its verdicts back in."""
    from . import ai as ai_mod

    candidates = list(result.hits)
    rescued: list[Listing] = []
    if rescue:
        # Listings the rules dropped *only* because the model is not in the
        # catalogue - a new card, or an odd spelling. Everything else stays
        # rejected; the AI does not get to overrule a price or VRAM filter.
        rescued = [l for l in result.rejected if l.rejected_reason == "no known GPU in title"]
        candidates += rescued

    if not candidates:
        return

    verdicts = validator.validate(candidates)
    result.ai_summary = validator.summary()

    for listing in candidates:
        verdict = verdicts.get(listing.key)
        if verdict is None:
            continue  # batch failed; the rule engine's decision stands

        was_rescued = listing in rescued
        if was_rescued:
            listing.rejected_reason = ""
        ai_mod.apply(listing, verdict)

        if listing.rejected_reason:
            if not was_rescued:
                result.hits.remove(listing)
                result.rejected.append(listing)
            continue

        if was_rescued:
            result.rejected.remove(listing)
            result.hits.append(listing)
        # VRAM or quantity may have been corrected, which moves the score.
        listing.score = score(listing)


def make_sources(names: list[str], fetcher, options: dict | None = None) -> list[Source]:
    return build(names, fetcher, options)
