"""Terminal output: the ranked table and the per-listing detail block."""

from __future__ import annotations

import json
import shutil

from .models import Listing

TIER_MARK = {"great": "***", "good": "** ", "ok": "*  ", "poor": "   "}


def _fit(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def table(listings: list[Listing], show_url: bool = True) -> str:
    if not listings:
        return "No matching listings."

    term_width = shutil.get_terminal_size((120, 24)).columns
    title_width = max(28, min(58, term_width - 62))

    header = (
        f"{'':3} {'SCORE':>6} {'PRICE':>10} {'kr/GB':>7} {'VRAM':>6}  "
        f"{'GPU':<18} {'TITLE':<{title_width}} {'WHERE':<14}"
    )
    lines = [header, "-" * len(header)]

    for listing in listings:
        gpu = listing.gpu
        price = f"{listing.price_sek:,.0f}".replace(",", " ") if listing.price_sek else "-"
        per_gb = f"{listing.kr_per_gb:,.0f}".replace(",", " ") if listing.kr_per_gb else "-"
        vram = f"{gpu.total_vram_gb} GB" if gpu else "-"
        name = gpu.name if gpu else "?"
        if gpu and gpu.quantity > 1:
            name = f"{gpu.quantity}x {name}"
        mark = TIER_MARK.get(gpu.ai_tier, "   ") if gpu else "   "
        lines.append(
            f"{mark} {listing.score:>6.1f} {price:>10} {per_gb:>7} {vram:>6}  "
            f"{_fit(name, 18):<18} {_fit(listing.title, title_width):<{title_width}} "
            f"{_fit(listing.source, 14):<14}"
        )
        auction = _auction_note(listing)
        if auction:
            lines.append(f"{'':>4}~ {auction}")
        flags = _flags(listing)
        if flags:
            lines.append(f"{'':>4}! {flags}")
        if show_url:
            lines.append(f"{'':>4}{listing.url}")

    lines.append("")
    lines.append("*** = ideal for local AI, ** = good, * = works with caveats")
    if any(l.price_is_provisional for l in listings):
        lines.append("~   = a running auction; the price shown is the current bid, not a cost")
    return "\n".join(lines)


def _auction_note(listing: Listing) -> str:
    """One line describing the bid state, for auctions only."""
    auction = listing.auction
    if not auction:
        return ""
    parts = []
    if auction["is_ended"]:
        parts.append("auction ended")
    else:
        parts.append("auction")
    bids = auction["bid_count"]
    parts.append(f"{bids} bid" + ("" if bids == 1 else "s"))
    if auction["next_bid"]:
        parts.append(f"next bid {auction['next_bid']:,.0f} kr".replace(",", " "))
    if auction["buy_now_price"]:
        parts.append(f"buy now {auction['buy_now_price']:,.0f} kr".replace(",", " "))
    if auction["ends_at"]:
        parts.append(f"ends {str(auction['ends_at'])[:16].replace('T', ' ')}")
    return ", ".join(parts)


def _flags(listing: Listing) -> str:
    """Warnings the AI validator raised, if it ran."""
    verdict = getattr(listing, "ai", None)
    if verdict is None:
        return ""
    flags = list(getattr(verdict, "risk_flags", []) or [])
    if getattr(verdict, "mining_risk", False):
        flags.insert(0, "möjligen minat")
    return ", ".join(flags)


def detail(listing: Listing) -> str:
    gpu = listing.gpu
    out = [listing.title, listing.url]
    if listing.price_sek is not None:
        line = f"Price: {listing.price_sek:,.0f} kr".replace(",", " ")
        if listing.currency != "SEK":
            line += f" (listed at {listing.price:,.2f} {listing.currency})".replace(",", " ")
        if listing.shipping_sek:
            line += f", shipping {listing.shipping_sek:,.0f} kr".replace(",", " ")
        if listing.import_cost_sek:
            line += f", import VAT+fee {listing.import_cost_sek:,.0f} kr".replace(",", " ")
        if listing.price_is_provisional:
            line += "  [current bid, not a fixed price]"
        out.append(line)
    auction = _auction_note(listing)
    if auction:
        out.append(f"Auction: {auction}")
    if gpu:
        out.append(
            f"GPU: {gpu.name} ({gpu.arch}, {gpu.vram_gb} GB"
            + (f" x{gpu.quantity}" if gpu.quantity > 1 else "")
            + (f", {gpu.bandwidth_gbs} GB/s" if gpu.bandwidth_gbs else "")
            + (f", {gpu.tdp_w} W" if gpu.tdp_w else "")
            + f") - rated '{gpu.ai_tier}' for local AI"
        )
        if gpu.confidence < 0.9:
            out.append(f"  ! VRAM size guessed from the model default (confidence {gpu.confidence:.0%})")
        if gpu.notes:
            out.append(f"  {gpu.notes}")
    verdict = getattr(listing, "ai", None)
    if verdict is not None:
        out.append(f"AI: {verdict.reason} (confidence {verdict.confidence:.0%})")
        flags = _flags(listing)
        if flags:
            out.append(f"  ! {flags}")
        if getattr(verdict, "condition", "unknown") != "unknown":
            out.append(f"  Condition: {verdict.condition}")
    if listing.location:
        out.append(f"Location: {listing.location}")
    if listing.posted_at:
        out.append(f"Posted: {listing.posted_at:%Y-%m-%d %H:%M}")
    return "\n".join(out)


def as_json(listings: list[Listing]) -> str:
    return json.dumps([listing.to_dict() for listing in listings], ensure_ascii=False, indent=2)


def as_csv(listings: list[Listing]) -> str:
    import csv
    import io

    columns = [
        "source", "title", "gpu", "vram_gb", "quantity", "price_sek",
        "kr_per_gb", "score", "is_auction", "bid_count", "ends_at", "location", "url",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    for listing in listings:
        gpu = listing.gpu
        auction = listing.auction
        writer.writerow(
            {
                "source": listing.source,
                "title": listing.title,
                "gpu": gpu.name if gpu else "",
                "vram_gb": gpu.total_vram_gb if gpu else "",
                "quantity": gpu.quantity if gpu else "",
                "price_sek": round(listing.price_sek) if listing.price_sek else "",
                "kr_per_gb": round(listing.kr_per_gb, 1) if listing.kr_per_gb else "",
                "score": listing.score,
                "is_auction": "yes" if auction else "",
                "bid_count": auction.get("bid_count", "") if auction else "",
                "ends_at": auction.get("ends_at", "") if auction else "",
                "location": listing.location,
                "url": listing.url,
            }
        )
    return buffer.getvalue()
