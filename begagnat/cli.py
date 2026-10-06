"""Command line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from . import report
from .config import Config
from .engine import run_search
from .fx import Fx
from .gpu_data import GPUS
from .http import Fetcher
from .query import Criteria
from .sources import ALL_SOURCES, DEFAULT_SOURCES, build
from .storage import DEFAULT_DB, Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="begagnat",
        description="Find high-VRAM GPUs for local AI on Swedish and international "
                    "second-hand marketplaces.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--config", help="path to a YAML config file")
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite database (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="run one search and print the results")
    _add_search_args(search)
    search.add_argument("--format", choices=["table", "json", "csv", "detail"], default="table")
    search.add_argument("--show-rejected", action="store_true", help="explain what was filtered out")
    search.add_argument("--no-store", action="store_true", help="do not record results in the database")

    watch = sub.add_parser("watch", help="poll repeatedly and notify on new hits")
    _add_search_args(watch)
    watch.add_argument("--interval", type=int, default=900, help="seconds between passes (default 900)")
    watch.add_argument("--once", action="store_true", help="run a single pass and exit")
    watch.add_argument("--profile", action="append", help="only run these config profiles")

    best = sub.add_parser("best", help="show the best deals recorded so far")
    best.add_argument("--limit", type=int, default=20)
    best.add_argument("--model", help="restrict to one GPU model")

    stats = sub.add_parser("stats", help="asking-price statistics per GPU model from your history")
    stats.add_argument("model", nargs="?", help="GPU model name (omit for all)")

    reindex = sub.add_parser(
        "reindex",
        help="re-run the title parser over stored listings (offline; no network)",
    )
    reindex.add_argument(
        "--dry-run", action="store_true", help="show what would change, and change nothing"
    )

    sub.add_parser("sources", help="list available marketplaces")
    gpus = sub.add_parser("gpus", help="list the GPU catalogue used for matching")
    gpus.add_argument("--min-vram", type=int, default=0)

    identify = sub.add_parser("identify", help="test the title parser on a string")
    identify.add_argument("title", nargs="+")

    login = sub.add_parser("login-marketplace", help="open a browser to log into Facebook once")
    login.add_argument("--profile-dir", help="where to keep the browser profile")

    web = sub.add_parser("web", help="start the local web UI (watches, feed, price history)")
    web.add_argument("--host", default="127.0.0.1",
                     help="bind address; use 0.0.0.0 to reach it from your phone on the LAN")
    web.add_argument("--port", type=int, default=8377)
    web.add_argument("--no-scheduler", action="store_true",
                     help="serve the UI but do not run watches in the background")
    web.add_argument("--open", action="store_true", help="open a browser window")

    return parser


def _add_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="*", help="free-text search terms")
    parser.add_argument(
        "--source", action="append", choices=sorted(ALL_SOURCES),
        help=f"marketplace to search (repeatable, default: {', '.join(DEFAULT_SOURCES)})",
    )
    parser.add_argument("--max-price", type=float, help="max landed price in SEK")
    parser.add_argument("--min-vram", type=int, help="minimum total VRAM in GB")
    parser.add_argument("--max-kr-per-gb", type=float, help="maximum price per GB of VRAM")
    parser.add_argument("--min-tier", choices=["poor", "ok", "good", "great"],
                        help="minimum suitability rating for local AI")
    parser.add_argument("--model", action="append", dest="models",
                        help="restrict to a GPU model from the catalogue (repeatable)")
    parser.add_argument("--exclude", action="append", default=[],
                        help="reject listings containing this word (repeatable)")
    parser.add_argument("--keyword", action="append", dest="keywords", default=[],
                        help="require this word in the listing (repeatable)")
    parser.add_argument("--limit", type=int, default=60, help="max listings per source")
    parser.add_argument("--region", help="source-specific region/county filter")
    parser.add_argument("--allow-broken", action="store_true",
                        help="keep listings marked broken/for parts/accessory")
    parser.add_argument("--any-listing", action="store_true",
                        help="keep listings whose GPU model is not in the catalogue")
    parser.add_argument("--no-import-cost", action="store_true",
                        help="compare raw prices without Swedish import VAT and fees")
    parser.add_argument("--ebay-domain", default=None,
                        help="eBay marketplace domain (default ebay.com, or source_options in the config)")
    parser.add_argument("--fb-city", default=None,
                        help="Facebook Marketplace city slug (default stockholm, or the config)")
    parser.add_argument("--fx", action="append", default=[], metavar="CUR=RATE",
                        help="override an exchange rate to SEK, e.g. --fx USD=11.1")
    parser.add_argument("--min-interval", type=float, default=1.5,
                        help="seconds between requests to the same host")
    parser.add_argument("--save-html", metavar="DIR",
                        help="dump raw responses here for debugging parsers")

    ai = parser.add_argument_group("AI validation (needs ANTHROPIC_API_KEY)")
    ai.add_argument("--ai", action="store_true",
                    help="have Claude read each listing and drop want-to-buy ads, "
                         "whole computers, scams, and misidentified cards")
    ai.add_argument("--ai-rescue", action="store_true",
                    help="also let the AI look at listings dropped for an unknown GPU "
                         "model (implies --ai)")
    ai.add_argument("--ai-model", default=None, help="model to validate with")
    ai.add_argument("--ai-batch", type=int, default=None,
                    help="listings per API call (default 8)")
    ai.add_argument("--ai-effort", choices=["low", "medium", "high"], default=None,
                    help="how hard the model should think (default low)")


def criteria_from_args(args) -> Criteria:
    return Criteria(
        query=" ".join(args.query) if args.query else "",
        keywords=args.keywords or [],
        sources=args.source or list(DEFAULT_SOURCES),
        max_price=args.max_price,
        min_vram=args.min_vram,
        max_kr_per_gb=args.max_kr_per_gb,
        min_tier=args.min_tier,
        models=args.models or [],
        exclude=args.exclude or [],
        allow_broken=args.allow_broken,
        require_known_gpu=not args.any_listing,
        limit=args.limit,
        region=args.region,
        include_import_cost=not args.no_import_cost,
    )


def fx_from_args(args, config: Config) -> Fx:
    fx = Fx.default().override(config.fx or {})
    pairs = {}
    for item in args.fx or []:
        currency, _, rate = item.partition("=")
        if not rate:
            raise SystemExit(f"--fx expects CUR=RATE, got {item!r}")
        pairs[currency] = float(rate)
    return fx.override(pairs)


def source_options(args, config: Config | None = None) -> dict:
    """Per-source settings: the config file underneath, command-line flags on top.

    Credentials belong in the config file rather than on a command line, where
    they would land in the shell history and in `ps`. A flag that was not given
    is dropped instead of overwriting the file with None.
    """
    merged = {name: dict(values) for name, values in ((config.source_options if config else {}) or {}).items()}
    flags = {
        "ebay": {"domain": getattr(args, "ebay_domain", None)},
        "marketplace": {"city": getattr(args, "fb_city", None)},
    }
    for name, values in flags.items():
        given = {key: value for key, value in values.items() if value is not None}
        merged[name] = {**merged.get(name, {}), **given}
    return merged


def make_validator(args, config: Config):
    """Build an AI validator, or None when --ai was not asked for."""
    if not (getattr(args, "ai", False) or getattr(args, "ai_rescue", False)):
        return None
    from .ai import DEFAULT_BATCH_SIZE, DEFAULT_MODEL, Validator

    settings = config.ai or {}
    return Validator(
        model=args.ai_model or settings.get("model", DEFAULT_MODEL),
        batch_size=args.ai_batch or int(settings.get("batch_size", DEFAULT_BATCH_SIZE)),
        effort=args.ai_effort or settings.get("effort", "low"),
    )


def make_fetcher(args) -> Fetcher:
    return Fetcher(
        min_interval=args.min_interval,
        cache_dir=Path(args.save_html) if getattr(args, "save_html", None) else None,
    )


# ---------------------------------------------------------------- commands
def cmd_search(args, config: Config) -> int:
    criteria = criteria_from_args(args)
    if not criteria.query and not criteria.models:
        raise SystemExit("give a search term, e.g. `begagnat search \"tesla p100\"`")

    fetcher = make_fetcher(args)
    sources = build(criteria.sources, fetcher, source_options(args, config))
    validator = make_validator(args, config)
    store = None if args.no_store else Store(args.db)
    if validator:
        validator.store = store
    try:
        result = run_search(
            criteria, sources, fx_from_args(args, config), store,
            validator=validator, rescue=args.ai_rescue,
        )
    finally:
        if store:
            store.close()

    for name, error in result.errors.items():
        print(f"! source {name} failed: {error}", file=sys.stderr)

    if args.format == "json":
        print(report.as_json(result.hits))
    elif args.format == "csv":
        print(report.as_csv(result.hits), end="")
    elif args.format == "detail":
        for listing in result.hits:
            print(report.detail(listing))
            print()
    else:
        print(report.table(result.hits))
        print(
            f"\n{len(result.hits)} hits out of {result.fetched} listings"
            f" ({len(result.new)} new since last run)."
        )
        if result.ai_summary:
            print(result.ai_summary)

    if args.show_rejected:
        print("\nFiltered out:")
        for listing in result.rejected:
            print(f"  - {listing.title[:70]:<70} {listing.rejected_reason}")

    return 0 if result.hits or not result.errors else 1


def cmd_watch(args, config: Config) -> int:
    from .notify import build_notifiers

    profiles = config.profiles
    if args.profile:
        wanted = set(args.profile)
        profiles = [p for p in profiles if p.name in wanted]
        if not profiles:
            raise SystemExit(f"no profile matched {', '.join(sorted(wanted))}")
    if not profiles:
        criteria = criteria_from_args(args)
        if not criteria.query and not criteria.models:
            raise SystemExit(
                "watch needs either a config file with profiles or a search term"
            )
        from .config import Profile

        profiles = [Profile(name=criteria.query or "cli", criteria=criteria)]

    notifiers = build_notifiers(config.notify)
    fx = fx_from_args(args, config)
    fetcher = make_fetcher(args)
    validator = make_validator(args, config)
    options = source_options(args, config)

    while True:
        with Store(args.db) as store:
            for profile in profiles:
                criteria = profile.criteria
                names = criteria.sources or list(DEFAULT_SOURCES)
                sources = build(names, fetcher, {**options, **profile.source_options})
                if validator:
                    validator.store = store
                result = run_search(
                    criteria, sources, fx, store,
                    validator=validator, rescue=args.ai_rescue,
                )

                for name, error in result.errors.items():
                    print(f"! [{profile.name}] source {name} failed: {error}", file=sys.stderr)

                announce = list(result.new)
                for listing, previous in result.price_drops:
                    listing.title = f"[PRISSÄNKT från {previous:,.0f} kr] {listing.title}".replace(",", " ")
                    announce.append(listing)

                stamp = time.strftime("%H:%M:%S")
                print(
                    f"[{stamp}] {profile.name}: {len(result.hits)} hits, "
                    f"{len(announce)} to report ({result.fetched} scanned)"
                    + (f" | {result.ai_summary}" if result.ai_summary else "")
                )
                if announce:
                    for notifier in notifiers:
                        notifier.send(announce)
                    store.mark_notified([listing.key for listing in announce])

        if args.once:
            return 0
        time.sleep(max(args.interval, 60))


def cmd_best(args, config: Config) -> int:
    with Store(args.db) as store:
        rows = store.best(args.limit, args.model)
    if not rows:
        print("Nothing recorded yet - run a search first.")
        return 0
    print(f"{'SCORE':>6} {'PRICE':>9} {'kr/GB':>7} {'VRAM':>6}  {'GPU':<18} TITLE")
    for row in rows:
        price = f"{row['price_sek']:,.0f}".replace(",", " ") if row["price_sek"] else "-"
        per_gb = f"{row['kr_per_gb']:,.0f}".replace(",", " ") if row["kr_per_gb"] else "-"
        print(
            f"{row['score']:>6.1f} {price:>9} {per_gb:>7} {str(row['vram_gb'] or '-'):>4} GB  "
            f"{(row['gpu_name'] or '?'):<18} {row['title'][:60]}"
        )
        print(f"       {row['url']}")
    return 0


def cmd_stats(args, config: Config) -> int:
    with Store(args.db) as store:
        names = [args.model] if args.model else sorted(
            {row["gpu_name"] for row in store.best(10_000) if row["gpu_name"]}
        )
        if not names:
            print("No price history yet.")
            return 0
        print(f"{'GPU':<22} {'N':>4} {'MIN':>9} {'MEDIAN':>9} {'MAX':>9}")
        for name in names:
            s = store.price_stats(name)
            if not s:
                continue
            print(
                f"{name:<22} {s['count']:>4} {s['min']:>9,.0f} {s['median']:>9,.0f} "
                f"{s['max']:>9,.0f}".replace(",", " ")
            )
    return 0


def cmd_reindex(args, config: Config) -> int:
    """Bring stored identifications up to date with the current parser.

    Every parser fix leaves history behind - a row keeps whatever was believed
    the day it was first seen. This is the only way to correct one without
    waiting for the ad to be re-scraped, which for a sold ad never happens.
    """
    with Store(args.db) as store:
        changes = store.reindex(dry_run=args.dry_run)

    if not changes:
        print("Every stored listing already matches the current parser.")
        return 0

    def describe(pair):
        name, vram = pair
        return f"{name} {vram} GB" if name else "no known GPU"

    for change in changes:
        print(f"{describe(change['was']):<24} -> {describe(change['now']):<24} {change['title'][:52]}")
    verb = "would change" if args.dry_run else "updated"
    print(f"\n{len(changes)} listing(s) {verb}.")
    return 0


def cmd_sources(args, config: Config) -> int:
    from .sources import resolve

    for name, cls in sorted(ALL_SOURCES.items()):
        default = " (default)" if name in DEFAULT_SOURCES else ""
        # eBay has two implementations; show the one this environment will use.
        active = resolve(name, (config.source_options or {}).get(name, {}))
        print(f"{name}{default}\n  {active.description}")
        if active is not cls:
            print(f"  (in place of: {cls.description})")
        if active.requires_setup:
            print(f"  setup: {active.requires_setup}")
    return 0


def cmd_gpus(args, config: Config) -> int:
    print(f"{'GPU':<24} {'VRAM':>10} {'BW GB/s':>8} {'TDP':>6} {'TIER':<6} NOTES")
    for gpu in sorted(GPUS, key=lambda g: (-g["vram_gb"], g["name"])):
        if gpu["vram_gb"] < args.min_vram:
            continue
        variants = "/".join(str(v) for v in gpu.get("vram_variants", [gpu["vram_gb"]]))
        print(
            f"{gpu['name']:<24} {variants + ' GB':>10} {gpu.get('bandwidth_gbs', '-'):>8} "
            f"{gpu.get('tdp_w', '-'):>6} {gpu.get('ai_tier', ''):<6} {gpu.get('notes', '')[:60]}"
        )
    return 0


def cmd_identify(args, config: Config) -> int:
    from .gpu import find_negatives, identify

    title = " ".join(args.title)
    match = identify(title)
    if not match:
        print(f"No catalogue GPU recognised in {title!r}")
    else:
        print(
            f"{match.name}: {match.vram_gb} GB x{match.quantity} = {match.total_vram_gb} GB, "
            f"{match.arch}, tier '{match.ai_tier}', confidence {match.confidence:.0%}"
        )
        if match.notes:
            print(f"  {match.notes}")
    negatives = find_negatives(title)
    if negatives:
        print(f"  ! would be filtered out by: {', '.join(negatives)}")
    return 0


def cmd_login_marketplace(args, config: Config) -> int:
    from .sources.marketplace import DEFAULT_PROFILE

    profile = Path(args.profile_dir or DEFAULT_PROFILE)
    profile.mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "playwright is not installed. Run:\n"
            "  pip install 'begagnat-scraper[browser]'\n"
            "  playwright install chromium"
        )

    print(f"Opening a browser. Log into Facebook, then close the window.\nProfile: {profile}")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com/marketplace/")
        input("Press Enter here when you are logged in... ")
        context.close()
    print("Saved. The marketplace source can use this profile now.")
    return 0


def cmd_web(args, config: Config) -> int:
    """Serve the UI, and run the watch scheduler alongside it unless told not to."""
    try:
        from .web import serve
    except ImportError as exc:
        raise SystemExit(
            "the web UI needs FastAPI and uvicorn: pip install -e '.[web]'"
        ) from exc

    url = f"http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    print(f"begagnat web UI on {url}  (db: {args.db or DEFAULT_DB})")
    if args.host == "0.0.0.0":
        print("Bound to every interface - anyone on your network can read your watches.")
    if args.open:
        import webbrowser

        webbrowser.open(url)
    serve(
        args.db or DEFAULT_DB,
        config,
        host=args.host,
        port=args.port,
        run_scheduler=not args.no_scheduler,
    )
    return 0


COMMANDS = {
    "search": cmd_search,
    "watch": cmd_watch,
    "best": cmd_best,
    "stats": cmd_stats,
    "reindex": cmd_reindex,
    "sources": cmd_sources,
    "gpus": cmd_gpus,
    "identify": cmd_identify,
    "login-marketplace": cmd_login_marketplace,
    "web": cmd_web,
}


def _use_utf8_output() -> None:
    """Swedish ads are full of aa/ae/oe; a cp1252 console mangles them.

    Windows still hands Python a legacy code page unless the user opts in, and
    the listing titles are the whole point of the output. Reconfigure rather
    than let `report` decide what to drop.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # a pipe that refuses to be re-wrapped
                pass


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = Config.load(args.config)
    try:
        return COMMANDS[args.command](args, config)
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
