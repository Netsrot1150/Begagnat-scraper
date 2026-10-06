"""Turn a free-text listing title into a concrete GPU with a VRAM figure.

Marketplace titles are written by humans in a hurry: "2 st Nvidia Tesla P100
16gb HBM2 servergrafikkort", "P 40 24GB", "RTX3090 defekt". This module
normalises that mess into a :class:`GpuMatch`, or explains why the listing was
thrown away.
"""

from __future__ import annotations

import re
import unicodedata

from .gpu_data import ACCESSORY_HINTS, CONDITION_HINTS, FORM_FACTOR_HINTS, GPUS
from .models import GpuMatch

# "16gb", "16 GB", "16g", "24-gb"
_VRAM_RE = re.compile(r"(?<![\d.,])(\d{1,3})\s*[- ]?\s*(?:gb|g\b|gigabyte)", re.I)
# "2st", "2 st", "2 stycken", "2 pcs" - a counting word, unambiguous anywhere.
_QTY_PREFIX_RE = re.compile(r"(?:^|[\s(\[])(\d{1,2})\s*(?:st\b|stycken\b|pcs\b|pieces\b)", re.I)
# "2x RTX 3090". A bare x is also a fan count in cooler names ("Ventus 2X OC",
# "Windforce 3X"), so it only counts at the head of the title, where a seller
# announces a bundle. Undercounting is the safe error: a phantom second card
# doubles the VRAM and defeats --min-vram.
_QTY_BARE_X_RE = re.compile(r"(?:^|[\s(\[])(\d{1,2})\s*x(?![\d])", re.I)
_QTY_BARE_X_HEAD_WORDS = 3
# A bare "x" is also a word in model names ("Gaming X 12GB", "Dual X 8G"), so
# the number after it must not be a size - otherwise a 12 GB card reads as
# twelve cards.
_QTY_SUFFIX_RE = re.compile(r"\bx\s?(\d{1,2})(?![\d])(?!\s*-?\s*(?:gb|g\b|gig|mb|tb))", re.I)
# "PCIe 4.0 x16", "pci-e x8", "PCIe-x16": a lane count, never a number of
# cards. Sellers put a hyphen anywhere in the phrase, so every gap tolerates
# one. A "PCIe-x16" read as sixteen cards claimed 256 GB on a 16 GB card.
_GAP = r"[\s\-]*"
_PCIE_LANES_RE = re.compile(
    rf"pci{_GAP}e(?:xpress)?{_GAP}(?:\d(?:\.\d)?{_GAP})?x?{_GAP}\d{{1,2}}", re.I
)


def normalise(text: str) -> str:
    """Lower-case, strip accents, and collapse separators so aliases match."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = text.replace("geforce", " ").replace("nvidia", " ").replace("amd", " ")
    text = re.sub(r"[_/|,]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


#: Words that turn a model number into a different card with different VRAM.
_VARIANT_SUFFIXES = ("ti", "super", "xt", "xtx")


def _alias_pattern(alias: str) -> re.Pattern:
    """Build a tolerant pattern for an alias.

    "rtx 3090" also matches "rtx3090"; "p100" also matches "p 100"; and the
    match must not run into a longer number, so "p100" never matches "p1000".

    A trailing variant word is also a different card: "RTX 3060 Ti" is an 8 GB
    card, not the 12 GB RTX 3060, so a plain alias refuses to swallow one. The
    variants worth cataloguing have their own alias ("rtx 3090 ti"), and the
    longest alias is tried first, so those still match.
    """
    parts = [re.escape(p) for p in alias.split()]
    body = r"\s*".join(parts)
    body = re.sub(r"([a-z])\\?\s\*(\d)", r"\1\\s*\2", body)
    tail = r"(?![a-z0-9])"
    if not alias.endswith(_VARIANT_SUFFIXES):
        tail += rf"(?!\s*(?:{'|'.join(_VARIANT_SUFFIXES)})\b)"
    return re.compile(rf"(?<![a-z0-9]){body}{tail}", re.I)


_PATTERNS: list[tuple[re.Pattern, dict, str]] = []
for _gpu in GPUS:
    for _alias in sorted(_gpu["aliases"], key=len, reverse=True):
        _PATTERNS.append((_alias_pattern(_alias), _gpu, _alias))
# Longest alias first: "rtx 4060 ti 16gb" must win over "4060 ti".
_PATTERNS.sort(key=lambda item: len(item[2]), reverse=True)


# Hints are matched against normalised text, so they have to be normalised too -
# otherwise "för delar" could never match "for delar".
_ACCESSORY = [(normalise(h), h) for h in ACCESSORY_HINTS]
_ANYWHERE = [(normalise(h), h) for h in CONDITION_HINTS + FORM_FACTOR_HINTS]


def find_negatives(title: str, description: str = "") -> list[str]:
    """Hints that this listing is a broken card, an accessory, or a server module.

    Accessory words are only believed in the title: plenty of good listings
    mention a cooler or a shroud in the body text ("passivt kylt, behöver
    fläktshroud") without being one.
    """
    in_title = normalise(title)
    everywhere = normalise(f"{title} {description}")
    hits = [original for needle, original in _ACCESSORY if needle in in_title]
    hits += [original for needle, original in _ANYWHERE if needle in everywhere]
    return hits


def extract_quantity(text: str) -> int:
    """How many cards the listing bundles. Defaults to 1."""
    low = _PCIE_LANES_RE.sub(" ", normalise(text))
    for regex in (_QTY_PREFIX_RE, _QTY_SUFFIX_RE, _QTY_BARE_X_RE):
        m = regex.search(low)
        if not m:
            continue
        if regex is _QTY_BARE_X_RE and low.count(" ", 0, m.start()) >= _QTY_BARE_X_HEAD_WORDS:
            continue
        qty = int(m.group(1))
        if 1 <= qty <= 16:
            return qty
    return 1


def extract_vram(text: str, allowed: list[int] | None = None) -> int | None:
    """Pick the VRAM figure out of a title.

    Only values that the model actually shipped in are accepted, which filters
    out noise like "PCIe 4.0" or a 500 GB disk in a bundle listing.
    """
    candidates = [int(m.group(1)) for m in _VRAM_RE.finditer(text or "")]
    if allowed:
        for value in candidates:
            if value in allowed:
                return value
        return None
    for value in candidates:
        if 2 <= value <= 200:
            return value
    return None


def identify(title: str, description: str = "") -> GpuMatch | None:
    """Best-effort identification of the GPU a listing is selling.

    The title decides the model (descriptions mention other cards too often);
    the description is only consulted for the VRAM variant and the quantity.
    """
    haystack = normalise(title)
    if not haystack:
        return None

    for pattern, gpu, alias in _PATTERNS:
        if not pattern.search(haystack):
            continue

        variants = gpu.get("vram_variants") or [gpu["vram_gb"]]
        vram = extract_vram(title, variants)
        confidence = 1.0
        if vram is None and description:
            vram = extract_vram(description, variants)
            confidence = 0.9
        if vram is None:
            vram = gpu["vram_gb"]
            # Ambiguous multi-variant cards (M40 12/24, V100 16/32) are a
            # guess until the seller says otherwise.
            confidence = 0.7 if len(variants) > 1 else 0.95

        quantity = extract_quantity(title)
        if quantity == 1 and description:
            quantity = extract_quantity(description)

        return GpuMatch(
            name=gpu["name"],
            vendor=gpu["vendor"],
            arch=gpu["arch"],
            vram_gb=vram,
            quantity=quantity,
            ai_tier=gpu.get("ai_tier", "ok"),
            bandwidth_gbs=gpu.get("bandwidth_gbs"),
            tdp_w=gpu.get("tdp_w"),
            compute=gpu.get("compute"),
            notes=gpu.get("notes", ""),
            confidence=confidence,
        )
    return None


def known_models() -> list[str]:
    return [gpu["name"] for gpu in GPUS]


def lookup(name: str) -> dict | None:
    """Find a catalogue entry by name or alias."""
    low = normalise(name)
    for gpu in GPUS:
        if normalise(gpu["name"]) == low or low in [normalise(a) for a in gpu["aliases"]]:
            return gpu
    return None
