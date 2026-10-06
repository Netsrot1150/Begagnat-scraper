"""Core data types shared by every source."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


@dataclass
class GpuMatch:
    """What we believe a listing is actually selling."""

    name: str
    vendor: str
    arch: str
    vram_gb: int
    quantity: int = 1
    ai_tier: str = "ok"
    bandwidth_gbs: int | None = None
    tdp_w: int | None = None
    compute: float | None = None
    notes: str = ""
    confidence: float = 1.0

    @property
    def total_vram_gb(self) -> int:
        return self.vram_gb * self.quantity


@dataclass
class Listing:
    """One advertisement, normalised across marketplaces."""

    source: str
    external_id: str
    title: str
    url: str
    price: float | None                  # in the listing's own currency
    currency: str = "SEK"
    price_sek: float | None = None       # converted, incl. import cost when applicable
    location: str = ""
    seller: str = ""
    posted_at: datetime | None = None
    description: str = ""
    image_url: str = ""
    shipping: float | None = None       # in the listing's own currency
    shipping_sek: float | None = None   # derived during analysis
    import_cost_sek: float | None = None
    raw: dict = field(default_factory=dict)

    # filled in by the analyser
    gpu: GpuMatch | None = None
    ai: "object | None" = None      # begagnat.ai.Verdict, when --ai is used
    rejected_reason: str = ""
    score: float = 0.0
    first_seen: datetime | None = None

    @property
    def key(self) -> str:
        """Stable identity used for de-duplication across runs."""
        return f"{self.source}:{self.external_id}"

    @property
    def fingerprint(self) -> str:
        """Identity that survives a re-listing of the same item by the same seller."""
        base = f"{self.source}|{self.title.strip().lower()}|{self.price}|{self.location.lower()}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    @property
    def kr_per_gb(self) -> float | None:
        if not self.gpu or not self.price_sek or self.gpu.total_vram_gb <= 0:
            return None
        return self.price_sek / self.gpu.total_vram_gb

    @property
    def auction(self) -> dict:
        """The bid state a source recorded, as a flat dict (empty if none).

        Sources put their own shape in ``raw``; this is the subset every part
        of the program agrees on, and the only part that survives into SQLite.
        """
        raw = self.raw or {}
        if not raw.get("is_auction"):
            return {}
        return {
            "is_auction": True,
            "is_ended": bool(raw.get("is_ended")),
            "bid_count": raw.get("bid_count") or 0,
            "current_bid": raw.get("current_bid"),
            "buy_now_price": raw.get("buy_now_price"),
            "next_bid": raw.get("next_bid"),
            "ends_at": raw.get("ends_at") or "",
        }

    @property
    def price_is_provisional(self) -> bool:
        """True when ``price`` is a standing bid rather than a cost.

        A running auction's bid is a floor: nobody has agreed to sell at it,
        and an auction that opens at 1 kr would otherwise be the best deal in
        the country until it closes. Such a listing is still shown - it may be
        the find of the week - but it is ranked after real prices and kept out
        of the price statistics.
        """
        raw = self.raw or {}
        if not raw.get("is_auction") or raw.get("is_ended"):
            return False
        return not raw.get("buy_now_price")

    def to_dict(self) -> dict:
        d = asdict(self)
        # `raw` is source-specific and can hold anything; only the agreed
        # auction subset is worth persisting, and the UI needs it to say
        # "bud, 3 dagar kvar" instead of pretending it is a price.
        d.pop("raw", None)
        d["auction"] = self.auction
        d["price_is_provisional"] = self.price_is_provisional
        if self.ai is not None and hasattr(self.ai, "to_dict"):
            d["ai"] = self.ai.to_dict()
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["first_seen"] = self.first_seen.isoformat() if self.first_seen else None
        d["kr_per_gb"] = round(self.kr_per_gb, 1) if self.kr_per_gb else None
        return d


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
