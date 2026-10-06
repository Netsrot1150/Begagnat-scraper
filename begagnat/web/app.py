"""JSON API + static frontend for the local web UI.

Everything here is a thin layer over the same engine the CLI uses. The rules
that decide what a good deal is live in `query.py`; this module only moves them
across HTTP.

Two things to know before editing:

* Each request opens its own `Store`. SQLite is in WAL mode, so the background
  scheduler can write while requests read, but a connection must not be shared
  across threads.
* Ad-hoc searches run in a worker thread with `run_in_threadpool`, because
  `run_search` is blocking and would otherwise stall the event loop for the
  20-30 seconds a three-marketplace fan-out takes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import gpu_data
from ..config import Config, criteria_from_dict
from ..engine import make_sources, run_search
from ..fx import Fx
from ..http import Fetcher
from ..models import Listing
from ..sources import ALL_SOURCES, DEFAULT_SOURCES
from ..sources import blocket_regions
from ..storage import DEFAULT_DB, Store
from ..watchrunner import Scheduler, run_watch, watch_to_dict

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Anything longer than this and the browser has given up anyway; the fetcher's
# own retries are what make a slow marketplace worth waiting for at all.
SEARCH_TIMEOUT_S = 120


def create_app(
    db_path: Path | str = DEFAULT_DB,
    config: Config | None = None,
    *,
    run_scheduler: bool = True,
) -> FastAPI:
    config = config or Config()
    db_path = Path(db_path)
    scheduler = Scheduler(db_path, config) if run_scheduler else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if scheduler:
            scheduler.start()
            log.info("watch scheduler started")
        yield
        if scheduler:
            scheduler.stop()

    app = FastAPI(
        title="begagnat",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.db_path = db_path
    app.state.config = config
    app.state.scheduler = scheduler

    def store() -> Store:
        return Store(db_path)

    # ------------------------------------------------------------------ meta

    @app.get("/api/status")
    def status() -> dict:
        with store() as db:
            watches = db.list_watches()
            return {
                "db": str(db_path),
                "watches": len(watches),
                "enabled_watches": sum(1 for w in watches if w["enabled"]),
                "unseen_events": db.unseen_event_count(),
                "scheduler_running": bool(scheduler and scheduler.alive),
                "scheduler_busy_with": scheduler.running_watch if scheduler else None,
                "sources": [
                    {
                        "name": name,
                        "description": cls.description,
                        "default": name in DEFAULT_SOURCES,
                    }
                    for name, cls in sorted(ALL_SOURCES.items())
                ],
                "ai_configured": bool(config.ai),
                # Blocket is the only source with a region filter today.
                "regions": blocket_regions.names(),
            }

    @app.get("/api/gpus")
    def gpus(min_vram: int = 0) -> list[dict]:
        cards = [g for g in gpu_data.GPUS if _max_vram(g) >= min_vram]
        cards.sort(key=lambda g: (-_max_vram(g), g["name"]))
        return [
            {
                "name": card["name"],
                "vendor": card.get("vendor", ""),
                "arch": card.get("arch", ""),
                "vram_gb": card.get("vram_gb"),
                "vram_variants": card.get("vram_variants") or [],
                "ai_tier": card.get("ai_tier", "ok"),
                "bandwidth_gbs": card.get("bandwidth_gbs"),
                "tdp_w": card.get("tdp_w"),
                "notes": card.get("notes", ""),
            }
            for card in cards
        ]

    # -------------------------------------------------------------- listings

    @app.get("/api/listings")
    def listings(
        limit: int = 200,
        gpu: str | None = None,
        source: str | None = None,
        max_price: float | None = None,
        min_vram: int | None = None,
        max_kr_per_gb: float | None = None,
        starred: bool = False,
        include_hidden: bool = False,
        watch_id: int | None = None,
    ) -> list[dict]:
        with store() as db:
            flags = db.flags()
            keys = set(db.watch_hit_keys(watch_id)) if watch_id is not None else None
            rows = db.conn.execute(
                "SELECT * FROM listings ORDER BY score DESC, kr_per_gb ASC LIMIT ?",
                (max(limit * 4, limit),),
            ).fetchall()

            out = []
            for row in rows:
                flag = flags.get(row["key"], {})
                if flag.get("hidden") and not include_hidden:
                    continue
                if starred and not flag.get("starred"):
                    continue
                if keys is not None and row["key"] not in keys:
                    continue
                if gpu and row["gpu_name"] != gpu:
                    continue
                if source and row["source"] != source:
                    continue
                if max_price is not None and (row["price_sek"] or 0) > max_price:
                    continue
                if min_vram is not None and (row["vram_gb"] or 0) < min_vram:
                    continue
                if max_kr_per_gb is not None and (row["kr_per_gb"] or 1e9) > max_kr_per_gb:
                    continue
                out.append(_row_to_dict(row, flag))
                if len(out) >= limit:
                    break
            return out

    @app.get("/api/listings/{key:path}")
    def listing_detail(key: str) -> dict:
        with store() as db:
            row = db.conn.execute("SELECT * FROM listings WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise HTTPException(404, f"unknown listing {key}")
            data = _row_to_dict(row, db.flags().get(key, {}))
            data["price_history"] = [
                {"at": at, "price_sek": price} for at, price in db.price_series(key)
            ]
            if row["gpu_name"]:
                data["model_prices"] = db.model_price_window(row["gpu_name"])
            return data

    @app.post("/api/listings/{key:path}/flags")
    def set_flags(key: str, body: dict = Body(...)) -> dict:
        fields = {k: v for k, v in body.items() if k in Store.FLAG_FIELDS}
        if not fields:
            raise HTTPException(400, f"nothing to set; expected any of {Store.FLAG_FIELDS}")
        with store() as db:
            db.set_flag(key, **fields)
            return db.flags().get(key, {})

    # ---------------------------------------------------------------- events

    @app.get("/api/events")
    def events(limit: int = 100, unseen_only: bool = False) -> list[dict]:
        with store() as db:
            names = {w["id"]: w["name"] for w in db.list_watches()}
            return [
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "key": row["key"],
                    "watch_id": row["watch_id"],
                    "watch_name": names.get(row["watch_id"], ""),
                    "created_at": row["created_at"],
                    "seen": bool(row["seen"]),
                    "payload": json.loads(row["payload"] or "{}"),
                }
                for row in db.events(limit=limit, unseen_only=unseen_only)
            ]

    @app.post("/api/events/seen")
    def mark_seen(body: dict = Body(default={})) -> dict:
        ids = body.get("ids")
        with store() as db:
            db.mark_events_seen(ids)
            return {"unseen": db.unseen_event_count()}

    # --------------------------------------------------------------- watches

    @app.get("/api/watches")
    def list_watches() -> list[dict]:
        with store() as db:
            return [watch_to_dict(row) for row in db.list_watches()]

    @app.post("/api/watches")
    def create_watch(body: dict = Body(...)) -> dict:
        return _save_watch(body, None)

    @app.put("/api/watches/{watch_id}")
    def update_watch(watch_id: int, body: dict = Body(...)) -> dict:
        return _save_watch(body, watch_id)

    def _save_watch(body: dict, watch_id: int | None) -> dict:
        name = (body.get("name") or "").strip()
        criteria = body.get("criteria") or {}
        if not name:
            raise HTTPException(400, "a watch needs a name")
        if not criteria.get("query") and not criteria.get("models"):
            raise HTTPException(400, "a watch needs a query or at least one model")
        with store() as db:
            if watch_id is not None and db.get_watch(watch_id) is None:
                raise HTTPException(404, f"unknown watch {watch_id}")
            try:
                new_id = db.save_watch(
                    name,
                    _clean_criteria(criteria),
                    watch_id=watch_id,
                    source_options=body.get("source_options"),
                    interval_s=int(body.get("interval_s", 900)),
                    enabled=bool(body.get("enabled", True)),
                    use_ai=bool(body.get("use_ai", False)),
                )
            except Exception as exc:  # a UNIQUE clash on the name is the likely one
                raise HTTPException(400, str(exc)) from exc
            return watch_to_dict(db.get_watch(new_id))

    @app.delete("/api/watches/{watch_id}")
    def delete_watch(watch_id: int) -> dict:
        with store() as db:
            if db.get_watch(watch_id) is None:
                raise HTTPException(404, f"unknown watch {watch_id}")
            db.delete_watch(watch_id)
            return {"deleted": watch_id}

    @app.post("/api/watches/{watch_id}/run")
    async def run_watch_now(watch_id: int) -> dict:
        def work() -> dict:
            with store() as db:
                row = db.get_watch(watch_id)
                if row is None:
                    raise HTTPException(404, f"unknown watch {watch_id}")
                result = run_watch(db, row, config=config)
                return {
                    "hits": len(result.hits),
                    "fetched": result.fetched,
                    "errors": result.errors,
                    "watch": watch_to_dict(db.get_watch(watch_id)),
                }

        return await run_in_threadpool(work)

    # ----------------------------------------------------------- ad-hoc search

    @app.post("/api/search")
    async def search(body: dict = Body(...)) -> dict:
        criteria_data = _clean_criteria(body.get("criteria") or body)
        if not criteria_data.get("query") and not criteria_data.get("models"):
            raise HTTPException(400, "give a search term or pick a model")
        persist = bool(body.get("store", True))

        def work() -> dict:
            criteria = criteria_from_dict(criteria_data)
            fetcher = Fetcher()
            db = store() if persist else None
            try:
                sources = make_sources(
                    criteria.sources or DEFAULT_SOURCES,
                    fetcher,
                    body.get("source_options") or {},
                )
                result = run_search(
                    criteria, sources, Fx.default().override(config.fx or {}), db
                )
                flags = db.flags() if db else {}
                return {
                    "hits": [_listing_to_dict(l, flags) for l in result.hits],
                    "rejected": [
                        {
                            "title": l.title,
                            "url": l.url,
                            "source": l.source,
                            "reason": l.rejected_reason,
                            "price_sek": l.price_sek,
                        }
                        for l in result.rejected
                    ],
                    "fetched": result.fetched,
                    "new": [l.key for l in result.new],
                    "errors": result.errors,
                }
            finally:
                fetcher.session.close()
                if db:
                    db.close()

        return await run_in_threadpool(work)

    # ------------------------------------------------------------------ stats

    @app.get("/api/stats")
    def stats(days: int = 180, model: str | None = Query(default=None)) -> list[dict]:
        with store() as db:
            names = [model] if model else db.known_models()
            out = []
            for name in names:
                window = db.model_price_window(name, days=days)
                if window:
                    out.append({"gpu": name, **window})
            out.sort(key=lambda row: row["median"])
            return out

    # ----------------------------------------------------------------- static

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(HTTPException)
    def http_error(_request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app


# ------------------------------------------------------------------ helpers

# Criteria fields the UI is allowed to set. Anything else in the body is
# ignored rather than rejected, so an older UI against a newer API still works.
_CRITERIA_FIELDS = {
    "query", "keywords", "sources", "max_price", "min_vram", "max_kr_per_gb",
    "min_tier", "models", "exclude", "allow_broken", "require_known_gpu",
    "limit", "region", "include_import_cost", "per_model_max_price",
}
_LIST_FIELDS = ("keywords", "sources", "models", "exclude")


def _clean_criteria(data: dict) -> dict:
    out = {k: v for k, v in data.items() if k in _CRITERIA_FIELDS and v not in ("", None)}
    for field in _LIST_FIELDS:
        value = out.get(field)
        if isinstance(value, str):
            out[field] = [part.strip() for part in value.split(",") if part.strip()]
    for field in ("max_price", "max_kr_per_gb"):
        if field in out:
            out[field] = float(out[field])
    for field in ("min_vram", "limit"):
        if field in out:
            out[field] = int(out[field])
    return out


def _max_vram(card: dict) -> int:
    variants = card.get("vram_variants") or []
    return max([card.get("vram_gb") or 0, *variants])


def _row_to_dict(row, flag: dict) -> dict:
    payload = json.loads(row["payload"] or "{}")
    return {
        "key": row["key"],
        "source": row["source"],
        "title": row["title"],
        "url": row["url"],
        "price_sek": row["price_sek"],
        "gpu": row["gpu_name"],
        "vram_gb": row["vram_gb"],
        "kr_per_gb": row["kr_per_gb"],
        "score": row["score"],
        "location": row["location"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "image_url": payload.get("image_url", ""),
        "import_cost_sek": payload.get("import_cost_sek"),
        "currency": payload.get("currency", "SEK"),
        "price": payload.get("price"),
        "ai": payload.get("ai"),
        "gpu_detail": payload.get("gpu"),
        # Older rows predate the auction block; an absent one reads as "not an
        # auction", which is what every fixed-price listing wants anyway.
        "auction": payload.get("auction") or {},
        "price_is_provisional": bool(payload.get("price_is_provisional")),
        "starred": bool(flag.get("starred")),
        "hidden": bool(flag.get("hidden")),
        "note": flag.get("note", ""),
    }


def _listing_to_dict(listing: Listing, flags: dict) -> dict:
    """A fresh search result, in the same shape a stored one comes back in.

    The frontend renders both through one function, so `gpu` has to be the
    model *name* here too, with the full match under `gpu_detail`.
    """
    data = listing.to_dict()
    flag = flags.get(listing.key, {})
    gpu = data.pop("gpu", None)
    data.update(
        key=listing.key,
        gpu=listing.gpu.name if listing.gpu else None,
        gpu_detail=gpu,
        vram_gb=listing.gpu.total_vram_gb if listing.gpu else None,
        last_seen=data.get("first_seen"),
        starred=bool(flag.get("starred")),
        hidden=bool(flag.get("hidden")),
        note=flag.get("note", ""),
    )
    return data


def serve(
    db_path: Path | str = DEFAULT_DB,
    config: Config | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8377,
    run_scheduler: bool = True,
) -> None:
    import uvicorn

    uvicorn.run(
        create_app(db_path, config, run_scheduler=run_scheduler),
        host=host,
        port=port,
        log_level="info",
    )
