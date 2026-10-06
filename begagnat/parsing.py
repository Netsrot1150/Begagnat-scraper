"""Shared helpers for pulling numbers and dates out of scraped markup."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

_PRICE_RE = re.compile(r"(\d[\d\s .,]*)")
_CURRENCY_HINTS = [
    ("SEK", ("kr", "sek", ":-")),
    ("EUR", ("eur", "€")),
    ("USD", ("usd", "us $", "$")),
    ("GBP", ("gbp", "£")),
]


def parse_price(text: str | None) -> tuple[float | None, str]:
    """Parse "1 234 kr", "SEK 1.234", "US $199.00" into ``(amount, currency)``.

    Swedish listings use a space (often non-breaking) as the thousands
    separator and a comma for decimals; eBay uses the opposite. The rule that
    works for both: the last separator is a decimal point only when it is
    followed by exactly two digits and the number has no other role for it.
    """
    if not text:
        return None, "SEK"

    low = text.lower()
    currency = "SEK"
    for code, hints in _CURRENCY_HINTS:
        if any(h in low for h in hints):
            currency = code
            break

    match = _PRICE_RE.search(text.replace(" ", " "))
    if not match:
        return None, currency

    raw = match.group(1).strip().rstrip(".,")
    raw = raw.replace(" ", "")
    if "," in raw and "." in raw:
        # Whichever separator comes last is the decimal one.
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in raw:
        raw = raw.replace(",", ".") if re.search(r",\d{1,2}$", raw) else raw.replace(",", "")
    elif "." in raw:
        if not re.search(r"\.\d{1,2}$", raw):
            raw = raw.replace(".", "")

    try:
        return float(raw), currency
    except ValueError:
        return None, currency


# Greedy on purpose: the amount must swallow every digit and separator it can,
# otherwise "$199.00" parses as 19.
_AMOUNT = r"\d[\d \u00a0.,]*\d|\d"
_MARKER = r"kr\b|:-|sek\b|eur\b|usd\b|gbp\b|€|\$|£"
_SUFFIX_PRICE_RE = re.compile(rf"({_AMOUNT})\s*({_MARKER})", re.I)
_PREFIX_PRICE_RE = re.compile(rf"({_MARKER})\s*({_AMOUNT})", re.I)


def find_price(text: str | None) -> tuple[float | None, str]:
    """Find a price inside a blob of card text.

    Unlike :func:`parse_price`, this refuses to read a bare number: the amount
    must sit next to a currency marker. Card text is full of decoys - a title
    like "Tesla V100 16GB" would otherwise parse as 10016.
    """
    if not text:
        return None, "SEK"
    for regex, amount_group in ((_SUFFIX_PRICE_RE, 1), (_PREFIX_PRICE_RE, 2)):
        match = regex.search(text)
        if match:
            marker = match.group(2 if amount_group == 1 else 1)
            amount, _ = parse_price(match.group(amount_group))
            _, currency = parse_price(f"{match.group(amount_group)} {marker}")
            return amount, currency
    return None, "SEK"


def parse_timestamp(value) -> datetime | None:
    """Accept epoch seconds/millis and the usual ISO-8601 spellings."""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:19]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def json_scripts(soup, script_id: str | None = None, script_type: str | None = None):
    """Yield decoded JSON from <script> blocks (``__NEXT_DATA__``, JSON-LD, ...)."""
    selector = {}
    if script_id:
        selector["id"] = script_id
    if script_type:
        selector["type"] = script_type
    for tag in soup.find_all("script", attrs=selector or None):
        text = tag.string or tag.get_text() or ""
        text = text.strip()
        if not text or text[0] not in "[{":
            continue
        try:
            yield json.loads(text)
        except (ValueError, TypeError):
            continue


def walk(node):
    """Depth-first walk over every dict and list inside a decoded JSON blob.

    Yields in document order, so callers that extract listings preserve the
    ordering the site returned (usually newest first). Iterative rather than
    recursive so deeply nested payloads cannot blow the stack.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))
