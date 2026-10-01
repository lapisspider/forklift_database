"""Split an optional leading OEM name off a lookup query: 'doosan L7-00116'
-> ('Bobcat/Doosan', 'L7-00116'). Bare serials (no OEM word) are untouched."""
from __future__ import annotations

import re

# Canonical DB manufacturer string -> alias words/phrases a user might type.
# Matched longest-phrase-first so 'big joe' wins over a bare 'big', etc.
ALIASES: dict[str, list[str]] = {
    "BYD": ["byd"],
    "Big Joe/EP Equipment": ["big joe", "bigjoe", "ep equipment", "ep"],
    "Bobcat/Doosan": ["bobcat", "doosan", "daewoo"],
    "CAT/Mitsubishi": ["cat", "caterpillar", "mitsubishi"],
    "Clark": ["clark"],
    "Club Car": ["club car", "clubcar"],
    "Crown": ["crown"],
    "Drexel": ["drexel"],
    "Heli": ["heli"],
    "Hyster": ["hyster"],
    "Hyundai": ["hyundai"],
    "Jungheinrich": ["jungheinrich"],
    "Komatsu": ["komatsu"],
    "Linde": ["linde"],
    "Nissan/Unicarrier/Logisnext": ["nissan", "unicarrier", "unicarriers", "logisnext", "tcm"],
    "Raymond": ["raymond"],
    "Stewart & Stevenson": ["stewart & stevenson", "stewart and stevenson", "stewart stevenson"],
    "Taylor Dunn": ["taylor dunn", "taylordunn"],
    "Textron": ["textron"],
    "Toyota": ["toyota"],
    "Yale": ["yale"],
    "Yamaha": ["yamaha"],
}

# Official sites used only to RESTRICT a spec-sheet search; no URL is ever built from these.
OEM_DOMAINS: dict[str, list[str]] = {
    "Bobcat/Doosan": ["bobcat.com"],
    "CAT/Mitsubishi": ["catlifttruck.com"],
    "Clark": ["clarkmhc.com"],
    "Crown": ["crown.com"],
    "Hyster": ["hyster.com"],
    "Jungheinrich": ["jungheinrich.com"],
    "Komatsu": ["komatsuforklift.com"],
    "Linde": ["linde-mh.com"],
    "Nissan/Unicarrier/Logisnext": ["unicarriers.com"],
    "Raymond": ["raymondcorp.com"],
    "Toyota": ["toyotaforklift.com"],
    "Yale": ["yale.com"],
}

_PAIRS = sorted(
    ((alias, canonical) for canonical, aliases in ALIASES.items() for alias in aliases),
    key=lambda p: (-len(p[0].split()), -len(p[0])),
)


def split_oem(raw: str) -> tuple[str | None, str]:
    """('doosan L7-00116') -> ('Bobcat/Doosan', 'L7-00116'). No match -> (None, raw)."""
    text = (raw or "").strip()
    lower = text.lower()
    for alias, canonical in _PAIRS:
        m = re.match(re.escape(alias) + r"(?=$|[^a-z0-9])", lower)
        if m:
            return canonical, text[m.end():].strip()
    return None, text
