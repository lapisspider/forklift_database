"""One plain-English lead sentence for a decoded serial, built only from fields
the decoder actually resolved. Returns (None, None) rather than a half-sentence."""
from __future__ import annotations

from ..schemas import SerialDecodeResult

_STATES = {"IL": "Illinois", "NJ": "New Jersey", "NC": "North Carolina", "AL": "Alabama", "KY": "Kentucky"}


def _place(plant: str | None, location: str | None) -> str | None:
    if plant == "G":
        return None  # code G is "government contracts", not a place
    if not location:
        return None
    parts = [p.strip() for p in location.split(",")]
    if len(parts) == 3 and parts[2] == "USA" and parts[1] in _STATES:
        return f"{parts[0]}, {_STATES[parts[1]]}"
    return location


def _or_join(years: list[int]) -> str:
    items = [str(y) for y in years]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _models(families: list[str]) -> str | None:
    """Distinct model designators from the decoder's family rows, or None when
    the rows name too many models to call it one truck."""
    rows = list(dict.fromkeys(f.strip() for f in families if f and f.strip()))
    if len(rows) != 1:
        return None
    parts = [p.strip() for p in rows[0].split(",") if p.strip()]
    if len(parts) <= 2:
        return " / ".join(parts)
    return f"{parts[0]} family"


def _hyster_yale(s: SerialDecodeResult) -> tuple[str | None, str | None]:
    brand = s.brand or "Hyster/Yale"
    if s.kind == "yale_pre1995":
        if not s.candidate_years:
            return None, None
        return f"Yale serial in a pre-1995 format — the year code gives {_or_join(s.candidate_years)}.", None
    if s.kind == "unrecognized" or not s.prefix:
        return None, None

    name = None if s.family_guess else _models(s.families)
    if s.kind in ("prefix", "unknown_prefix") and not name:
        if s.family_guess:
            return (f"{brand} prefix {s.prefix} — not on file; model family unconfirmed.", None)
        return None, None
    subject = f"{brand} {name}" if name else f"{brand} prefix {s.prefix}"
    if s.kind in ("prefix", "unknown_prefix"):
        return f"{subject} — model family from prefix {s.prefix}.", None

    place = _place(s.plant, s.plant_location)
    years = s.candidate_years
    note = None
    if s.family_guess:
        note = "The model family is inferred from the prefix digits only and is unconfirmed."
    elif len(years) > 1:
        note = "Confirm the exact year on the data plate."

    if place and len(years) == 1:
        tail = f"built at {place}, around {years[0]}"
    elif place and len(years) > 1:
        tail = f"built at {place}; the year letter gives {_or_join(years)}"
    elif place:
        tail = f"built at {place}; year not determined"
    elif len(years) == 1:
        tail = f"around {years[0]}"
    elif years:
        tail = f"the year letter gives {_or_join(years)}"
    else:
        tail = "year and plant not determined"
    return f"{subject} — {tail}.", note


def _doosan(s: SerialDecodeResult) -> tuple[str | None, str | None]:
    if s.kind not in ("doosan_serial", "doosan_prefix") or not s.prefix:
        return None, None
    name = _models(s.families)
    subject = f"Doosan {name}" if name else f"Doosan prefix {s.prefix}"
    if s.kind == "doosan_prefix":
        return f"{subject} — model family from prefix {s.prefix}.", None

    years = s.year_values
    basis = s.year_basis
    hint = "A batch-allocated hint, not a build date — verify on the data plate."
    if basis == "approximate" and years:
        lead, note = f"{subject} — approximately {years[0]}.", hint
    elif basis == "range" and years:
        span = str(years[0]) if years[0] == years[-1] else f"{years[0]}–{years[-1]}"
        lead, note = f"{subject} — approximately {span}.", hint
    elif basis == "at_or_before" and years:
        lead = f"{subject} — year not determined."
        note = f"At or before {years[0]}, the earliest batch record on file for this prefix."
    elif basis == "single_marker" and years:
        lead = f"{subject} — year not determined."
        note = f"Only one batch record on file ({years[0]}), so a year can't be bracketed."
    else:
        lead, note = f"{subject} — year not determined.", None
    if s.orphan:
        note = f"Prefix {s.prefix} is on the year table only, so engine and brake details aren't available."
    return lead, note


def summarize(s: SerialDecodeResult) -> tuple[str | None, str | None]:
    if s.kind.startswith("doosan"):
        return _doosan(s)
    return _hyster_yale(s)
