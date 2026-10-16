"""Cushion or pneumatic, read off the model code.

Why this is a parser and not a column: the trucks it matters for are the ones
NOT in the catalog. A cross-market counterpart is by definition a model this
database does not carry -- Yale ERP18VT and Hyster J1.8XN have no rows here --
so a stored field could never answer the question. The designation itself
carries the tire (Yale ER-C = Electric Rider Cushion, ER-P = Pneumatic), which
works for any model, on file or not, at no cost.

Why it matters: it is the difference the rest of the pipeline cannot see. The
capacity check passes for a cushion/pneumatic pair of the same rating, and OSHA
Class I covers every electric rider whatever its tire (CLAUDE.md), so a Yale
ERC040VAN and a Yale ERP18VT look identical on every field the catalog holds --
while being the wrong truck for a kit.

`None` is a normal and frequent answer. A wrong tire call is worse than no tire
call, because a parts dealer acts on it, so a family is only listed here once it
has been checked against the catalog. Every rule below agrees with the Class
IV/V rows (which encode tire for IC trucks by definition) and with the curated
platform_group twins: S40FT<->GC040VX and E80XN<->ERC080VH are cushion pairs,
H40XT<->GP040MX and J40XNT<->ERP040VT pneumatic ones.

Do NOT add an OEM without running audit_tire_class.py against it first. The
`CPD` trap in CLAUDE.md -- diesel on one Heli page, electric on another -- is
what guessing from a code string looks like when it goes wrong.
"""
from __future__ import annotations

import re

CUSHION = "Cushion"
PNEUMATIC = "Pneumatic"

_STRIP = re.compile(r"[\s\-_/.]")

# OEM -> {prefix: tire}. Matched longest prefix first (see _RULES), so "FGC"
# wins over "FG" and "2EPC" over "2EP".
_PREFIXES: dict[str, dict[str, str]] = {
    "Yale": {
        # Electric Rider Cushion / Pneumatic, Electric Stand-up Cushion.
        "ERC": CUSHION, "ESC": CUSHION, "ERP": PNEUMATIC,
        # IC: G<fuel>C cushion, G<fuel>P pneumatic. Bare GC/GP are gasoline.
        "GC": CUSHION, "GLC": CUSHION, "GDC": CUSHION,
        "GP": PNEUMATIC, "GLP": PNEUMATIC, "GDP": PNEUMATIC,
    },
    "Hyster": {
        # S and E are the cushion lines (IC and electric), H and J the
        # pneumatic ones. Confirmed by four curated twin pairs.
        "S": CUSHION, "E": CUSHION,
        "H": PNEUMATIC, "J": PNEUMATIC,
    },
    "CAT/Mitsubishi": {
        "C": CUSHION, "2C": CUSHION, "GC": CUSHION, "EC": CUSHION,
        "FGC": CUSHION, "FBC": CUSHION, "2EPC": CUSHION,
        "P": PNEUMATIC, "2P": PNEUMATIC, "GP": PNEUMATIC, "EP": PNEUMATIC,
        "2EP": PNEUMATIC, "FG": PNEUMATIC, "FD": PNEUMATIC, "FB": PNEUMATIC,
        "DP": PNEUMATIC,
    },
}

# Longest prefix first, so a shorter rule never shadows a longer one.
_RULES: dict[str, list[tuple[str, str]]] = {
    oem: sorted(table.items(), key=lambda kv: -len(kv[0]))
    for oem, table in _PREFIXES.items()
}


def normalize_model(model: str | None) -> str:
    """Uppercase, separators removed -- 'ETR 230' and 'GC35-50K5' compare cleanly."""
    return _STRIP.sub("", model or "").upper()


def tire_from_model(manufacturer: str | None, model: str | None) -> str | None:
    """"Cushion", "Pneumatic", or None when this OEM/model is not covered.

    None is the safe, common answer and callers must treat it as "unknown",
    never as "no difference".
    """
    rules = _RULES.get((manufacturer or "").strip())
    if not rules:
        return None
    norm = normalize_model(model)
    if not norm:
        return None
    for prefix, tire in rules:
        if norm.startswith(prefix):
            return tire
    return None


def tire_conflict(manufacturer_a: str | None, model_a: str | None,
                  manufacturer_b: str | None, model_b: str | None) -> tuple[str, str] | None:
    """(tire_a, tire_b) when both parse AND differ, else None.

    Only a confident disagreement counts. One side unknown is not a conflict --
    it is simply unknown, and saying otherwise would invent a warning.
    """
    a = tire_from_model(manufacturer_a, model_a)
    b = tire_from_model(manufacturer_b, model_b)
    if a and b and a != b:
        return a, b
    return None
