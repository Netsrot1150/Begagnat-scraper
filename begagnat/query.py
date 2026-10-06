"""Search criteria, listing analysis, and deal scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import gpu as gpu_mod
from .fx import Fx, landed_cost_sek
from .models import Listing

# Rough value of a card for local inference, independent of price. Bandwidth
# drives token generation, VRAM decides what fits at all, and the tier folds in
# software support (fp16/bf16, current CUDA/ROCm, FlashAttention).
_TIER_WEIGHT = {"great": 1.0, "good": 0.8, "ok": 0.55, "poor": 0.25}


@dataclass
class Criteria:
    """Everything a search run needs to know."""

    query: str = ""
    keywords: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    max_price: float | None = None            # SEK, landed
    min_vram: int | None = None
    max_kr_per_gb: float | None = None
    min_tier: str | None = None               # great|good|ok|poor
    models: list[str] = field(default_factory=list)   # restrict to these GPU names
    exclude: list[str] = field(default_factory=list)  # extra words that disqualify
    allow_broken: bool = False
    require_known_gpu: bool = True
    limit: int = 60
    region: str | None = None                 # source-specific region filter
    include_import_cost: bool = True
    per_model_max_price: dict[str, float] = field(default_factory=dict)

    def tier_ok(self, tier: str) -> bool:
        if not self.min_tier:
            return True
        order = ["poor", "ok", "good", "great"]
        return order.index(tier) >= order.index(self.min_tier)


def analyse(listing: Listing, criteria: Criteria, fx: Fx) -> Listing:
    """Identify the GPU, compute the landed price, and score the deal.

    Sets ``rejected_reason`` instead of raising, so a run can report *why*
    something was filtered out rather than silently dropping it.
    """
    text = f"{listing.title} {listing.description}"

    # Price first: it does not depend on the GPU, and a listing that gets
    # rejected below should still carry a price for the --show-rejected report
    # and for a later AI rescue.
    goods_sek = fx.to_sek(listing.price, listing.currency)
    if goods_sek is not None:
        listing.shipping_sek = fx.to_sek(listing.shipping, listing.currency)
        total, import_cost = landed_cost_sek(
            goods_sek,
            listing.shipping_sek or 0.0,
            country=listing.raw.get("country"),
            apply_import_cost=criteria.include_import_cost and listing.currency != "SEK",
        )
        listing.price_sek = round(total, 2)
        listing.import_cost_sek = round(import_cost, 2) or None

    listing.gpu = gpu_mod.identify(listing.title, listing.description)
    if criteria.require_known_gpu and not listing.gpu:
        listing.rejected_reason = "no known GPU in title"
        return listing

    negatives = gpu_mod.find_negatives(listing.title, listing.description)
    if negatives and not criteria.allow_broken:
        listing.rejected_reason = f"excluded by keyword: {', '.join(negatives[:3])}"
        return listing

    low = text.lower()
    hit = [word for word in criteria.exclude if word.lower() in low]
    if hit:
        listing.rejected_reason = f"excluded by keyword: {', '.join(hit)}"
        return listing

    if criteria.keywords and not all(
        re.search(re.escape(k), low, re.I) for k in criteria.keywords
    ):
        listing.rejected_reason = "missing required keyword"
        return listing

    if criteria.models and listing.gpu and listing.gpu.name not in criteria.models:
        listing.rejected_reason = f"{listing.gpu.name} not in --model filter"
        return listing

    if listing.price_sek is None:
        listing.rejected_reason = "no price (auction without bids, or 'ring för pris')"

    if listing.gpu:
        if criteria.min_vram and listing.gpu.total_vram_gb < criteria.min_vram:
            listing.rejected_reason = f"only {listing.gpu.total_vram_gb} GB VRAM"
            return listing
        if not criteria.tier_ok(listing.gpu.ai_tier):
            listing.rejected_reason = f"tier {listing.gpu.ai_tier} below --min-tier"
            return listing

    if listing.price_sek is not None:
        cap = criteria.max_price
        if listing.gpu:
            model_cap = criteria.per_model_max_price.get(listing.gpu.name)
            if model_cap is not None:
                # A per-model cap is written per card, so a 2-pack gets 2x the budget.
                model_cap *= listing.gpu.quantity
                cap = model_cap if cap is None else min(cap, model_cap)
        if cap is not None and listing.price_sek > cap:
            listing.rejected_reason = f"{listing.price_sek:.0f} kr over budget {cap:.0f} kr"
            return listing

        per_gb = listing.kr_per_gb
        if criteria.max_kr_per_gb and per_gb and per_gb > criteria.max_kr_per_gb:
            listing.rejected_reason = f"{per_gb:.0f} kr/GB over limit"
            return listing

    listing.score = score(listing)
    return listing


def score(listing: Listing) -> float:
    """Value per krona, normalised so ~100 is a genuinely good deal.

    The numerator is what the hardware is worth for inference (VRAM x a
    bandwidth factor x a software-support factor); the denominator is what it
    actually costs you, landed.
    """
    gpu = listing.gpu
    if not gpu or not listing.price_sek or listing.price_sek <= 0:
        return 0.0

    bandwidth_factor = ((gpu.bandwidth_gbs or 300) / 500) ** 0.5
    tier_factor = _TIER_WEIGHT.get(gpu.ai_tier, 0.5)
    value = gpu.total_vram_gb * bandwidth_factor * tier_factor
    raw = value / listing.price_sek * 1000 * gpu.confidence
    return round(raw * 10, 1)


def sort_key(listing: Listing):
    """Rank settled prices above running auctions, then by value per krona.

    An auction that opens at 1 kr scores better than anything else on the
    market until somebody bids, which made the top of the list useless. The
    bid is still scored - it just cannot outrank a price you could pay today.
    """
    return (listing.price_is_provisional, -listing.score, listing.kr_per_gb or float("inf"))
