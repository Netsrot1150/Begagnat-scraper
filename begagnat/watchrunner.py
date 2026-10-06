"""Runs stored watches ("bevakningar") and turns their results into events.

`cli watch` loops over YAML profiles; this module loops over watches stored in
SQLite, which is what the web UI edits. Both end up in `engine.run_search` - the
difference is only where the criteria come from and what happens afterwards.

A watch is deliberately *not* the same thing as a listing: two watches can match
the same ad, and each of them should announce it once. `Store.link_watch_hit`
owns that distinction.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from .config import Config, criteria_from_dict
from .engine import RunResult, make_sources, run_search
from .sources import DEFAULT_SOURCES
from .fx import Fx
from .http import Fetcher
from .storage import Store

log = logging.getLogger(__name__)

# A watch that keeps failing should not keep hammering the marketplace. After
# this many consecutive errors the runner backs off to a longer interval; the
# UI still shows the error so the user can fix the query.
BACKOFF_AFTER_ERRORS = 3
BACKOFF_FACTOR = 4


def watch_to_dict(row) -> dict:
    """A sqlite row for a watch, as JSON the UI can round-trip."""
    return {
        "id": row["id"],
        "name": row["name"],
        "criteria": json.loads(row["criteria"]),
        "source_options": json.loads(row["source_options"] or "{}"),
        "interval_s": row["interval_s"],
        "enabled": bool(row["enabled"]),
        "use_ai": bool(row["use_ai"]),
        "created_at": row["created_at"],
        "last_run_at": row["last_run_at"],
        "last_error": row["last_error"] or "",
        "last_hits": row["last_hits"],
    }


def run_watch(
    store: Store,
    row,
    *,
    config: Config | None = None,
    fetcher: Fetcher | None = None,
    notifiers: list | None = None,
) -> RunResult:
    """Run one watch and record what changed.

    Errors from a single source are already isolated by `run_search`; an error
    that takes the whole watch down (a bad query, a dead network) is written to
    `watches.last_error` and raised to the caller, which decides whether to keep
    the loop alive.
    """
    config = config or Config()
    criteria = criteria_from_dict(json.loads(row["criteria"]))
    # The config file underneath, the watch's own settings on top. Credentials
    # live in the config, so a watch created in the UI reaches the same APIs a
    # command-line search does without anyone typing a secret into a web form.
    source_options = {name: dict(values) for name, values in (config.source_options or {}).items()}
    for name, values in json.loads(row["source_options"] or "{}").items():
        source_options[name] = {**source_options.get(name, {}), **(values or {})}
    fx = Fx.default().override(config.fx or {})
    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher()

    validator = None
    if row["use_ai"]:
        validator = _build_validator(config, store)

    try:
        sources = make_sources(criteria.sources or DEFAULT_SOURCES, fetcher, source_options)
        result = run_search(
            criteria,
            sources,
            fx,
            store,
            validator=validator,
            rescue=bool(config.ai.get("rescue")),
        )
    except Exception as exc:  # noqa: BLE001 - a broken watch must not kill the loop
        store.finish_watch_run(row["id"], hits=0, error=str(exc))
        store.add_event("error", watch_id=row["id"], payload={"error": str(exc)})
        raise

    fresh = []
    for listing in result.hits:
        if store.link_watch_hit(row["id"], listing.key):
            fresh.append(listing)
            store.add_event(
                "new",
                watch_id=row["id"],
                key=listing.key,
                payload=_event_payload(listing),
            )

    for listing, previous in result.price_drops:
        store.add_event(
            "price_drop",
            watch_id=row["id"],
            key=listing.key,
            payload={**_event_payload(listing), "previous_price_sek": previous},
        )

    store.finish_watch_run(
        row["id"], hits=len(result.hits), error="; ".join(result.errors.values())
    )

    if notifiers and fresh:
        for notifier in notifiers:
            try:
                notifier.send(fresh)
            except Exception as exc:  # noqa: BLE001 - a dead webhook is not fatal
                log.warning("notifier %s failed: %s", type(notifier).__name__, exc)

    if own_fetcher:
        fetcher.session.close()
    return result


def _event_payload(listing) -> dict:
    gpu = listing.gpu
    return {
        "title": listing.title,
        "url": listing.url,
        "source": listing.source,
        "price_sek": listing.price_sek,
        "gpu": gpu.name if gpu else None,
        "vram_gb": gpu.total_vram_gb if gpu else None,
        "kr_per_gb": round(listing.kr_per_gb, 1) if listing.kr_per_gb else None,
        "score": listing.score,
    }


def _build_validator(config: Config, store: Store):
    """Build the Claude validator from the config's `ai:` block.

    `store` is passed in so verdicts stay cached across runs - a watch that
    fires every 15 minutes must not pay for the same unchanged ad each time.
    """
    from . import ai as ai_mod

    settings = {k: v for k, v in (config.ai or {}).items()
                if k in ("model", "batch_size", "effort")}
    return ai_mod.Validator(store=store, **settings)


class Scheduler:
    """A background thread that runs watches when they come due.

    It owns its own `Store`, so the web request handlers can keep using theirs
    without sharing a SQLite connection across threads.
    """

    def __init__(self, db_path, config: Config | None = None, *, tick_s: float = 20.0):
        self.db_path = db_path
        self.config = config or Config()
        self.tick_s = tick_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._errors: dict[int, int] = {}
        self.last_tick_at: float | None = None
        self.running_watch: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="begagnat-watches", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        from .notify import build_notifiers

        store = Store(self.db_path)
        fetcher = Fetcher()
        notifiers = build_notifiers(self.config.notify)
        try:
            while not self._stop.is_set():
                self.last_tick_at = time.time()
                try:
                    self._tick(store, fetcher, notifiers)
                except Exception as exc:  # noqa: BLE001 - the loop outlives any one failure
                    log.exception("watch scheduler tick failed: %s", exc)
                self._stop.wait(self.tick_s)
        finally:
            store.close()
            fetcher.session.close()

    def _tick(self, store: Store, fetcher: Fetcher, notifiers: list) -> None:
        for row in store.due_watches():
            if self._stop.is_set():
                return
            if self._backed_off(row):
                continue
            self.running_watch = row["name"]
            try:
                run_watch(store, row, config=self.config, fetcher=fetcher, notifiers=notifiers)
                self._errors.pop(row["id"], None)
            except Exception as exc:  # noqa: BLE001 - already recorded on the watch
                self._errors[row["id"]] = self._errors.get(row["id"], 0) + 1
                log.warning("watch %s failed: %s", row["name"], exc)
            finally:
                self.running_watch = None

    def _backed_off(self, row) -> bool:
        """Skip a watch that has failed repeatedly, most of the time."""
        failures = self._errors.get(row["id"], 0)
        if failures < BACKOFF_AFTER_ERRORS:
            return False
        # Run it every BACKOFF_FACTOR-th time it comes due instead of every time.
        return failures % BACKOFF_FACTOR != 0
