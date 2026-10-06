"""Blocket's `location` filter codes, one per Swedish county.

Captured live from the `location` facet of the recommerce search response on
2026-09-04. Blocket wants opaque codes like `0.300003`; the user wants to type
"Uppsala". Re-capture with:

    begagnat search grafikkort --source blocket --save-html html-dump/
    python -c "import json,sys;d=json.load(open(sys.argv[1]));\
        print([(i['display_name'], i['value']) for f in d['filters'] \
        if f['name']=='location' for i in f['filter_items']])" html-dump/<file>

Local pickup matters here: a 250 W passive server card is heavy, awkward and
often not worth shipping, so "only my county" is a real filter rather than a
convenience.
"""

from __future__ import annotations

import unicodedata

#: County name (lowercased) -> Blocket location code.
REGIONS: dict[str, str] = {
    "blekinge": "0.300010",
    "dalarna": "0.300020",
    "gotland": "0.300009",
    "gävleborg": "0.300021",
    "halland": "0.300013",
    "jämtland": "0.300023",
    "jönköping": "0.300006",
    "kalmar": "0.300008",
    "kronoberg": "0.300007",
    "norrbotten": "0.300025",
    "skåne": "0.300012",
    "stockholm": "0.300001",
    "södermanland": "0.300004",
    "uppsala": "0.300003",
    "värmland": "0.300017",
    "västerbotten": "0.300024",
    "västernorrland": "0.300022",
    "västmanland": "0.300019",
    "västra götaland": "0.300014",
    "örebro": "0.300018",
    "östergötland": "0.300005",
}

#: Names people actually type that are not the county's own name.
ALIASES: dict[str, str] = {
    "göteborg": "västra götaland",
    "gothenburg": "västra götaland",
    "malmö": "skåne",
    "scania": "skåne",
    "sthlm": "stockholm",
    "vgr": "västra götaland",
}


def _fold(text: str) -> str:
    """Lowercase and strip accents, so "Skane" finds "Skåne"."""
    lowered = (text or "").strip().lower()
    return "".join(
        ch for ch in unicodedata.normalize("NFD", lowered)
        if unicodedata.category(ch) != "Mn"
    )


_FOLDED = {_fold(name): code for name, code in REGIONS.items()}
_FOLDED_ALIASES = {_fold(alias): _fold(target) for alias, target in ALIASES.items()}


def code_for(region: str | None) -> str | None:
    """Translate a county name to Blocket's code.

    A value that already looks like a code is passed through untouched, so a
    future county or a code read straight off the site still works.
    """
    if not region:
        return None
    if region.startswith("0.3"):
        return region
    folded = _fold(region)
    folded = _FOLDED_ALIASES.get(folded, folded)
    return _FOLDED.get(folded)


def names() -> list[str]:
    return sorted(REGIONS)
