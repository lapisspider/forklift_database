"""Offline decoding of Hyster/Yale OEM serial numbers and prefixes.

Serial shape (post-1957 Hyster, post-Q3-1995 Yale): PPPP L NNNNN Y
  PPPP = design-generation letter + 3-digit model family (chars 1-4)
  L    = plant letter (char 5)
  NNNNN = production sequence at that plant (chars 6-10)
  Y    = year letter, 23-letter alphabet, repeats every 23 years (char 11)
Never touches the web -- this is purely local DB + JSON reference data.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Forklift, SerialPlantCode, SerialPrefix, SerialYearCode
from ..schemas import SerialDecodeResult, SerialMatch

_REF_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "reference"
_PRE1995 = json.loads((_REF_DIR / "yale_pre1995.json").read_text(encoding="utf-8"))

_NORM = lambda s: re.sub(r"[\s\-_/.]", "", s or "").upper()

YEAR = "ABCDEFGHJKLMNPRSTUVWXYZ"
PREFIX_RE = re.compile(r"^[A-Z][0-9][A-Z0-9][0-9]$")
SERIAL_RE = re.compile(
    r"^(?P<prefix>[A-Z][0-9][A-Z0-9][0-9])"
    r"(?P<plant>[A-Z])(?P<seq>[0-9]{5})(?P<year>[ABCDEFGHJKLMNPRSTUVWXYZ])$"
)
YALE_OLD_RE = re.compile(r"^(?:[0-9]{6}|(?:H|J|A[B-K])[0-9]{3,6})$")

CORRECTION_NOTE = (
    "Published Hyster charts run a year late for 2017 onward (the source lists "
    "Q=2017 and shifts every later year by one, but Q is never used). This app "
    "uses the corrected sequence (R=2017 ... A=2026) — verify against the data plate."
)

RANGE_RE = re.compile(r"^([A-Z]+)(\d+)-(\d+)([A-Z0-9]*)$")


def normalize(raw: str) -> str:
    return _NORM(raw)


def classify(raw: str) -> str | None:
    """'serial' | 'prefix' | 'yale_pre1995' | None (not recognized)."""
    s = normalize(raw)
    if not s:
        return None
    if SERIAL_RE.match(s):
        return "serial"
    if PREFIX_RE.match(s):
        return "prefix"
    if YALE_OLD_RE.match(s):
        return "yale_pre1995"
    return None


def _model_index(db: Session) -> dict[tuple[str, str], int]:
    idx = {}
    for manufacturer, model, fid in db.query(Forklift.manufacturer, Forklift.model, Forklift.id):
        idx[(manufacturer, _NORM(model))] = fid
    return idx


def _expand(token: str) -> list[str]:
    """'S80-120FT' -> ['S80FT','S90FT',...,'S120FT']. Non-range or decimal-metric
    tokens (e.g. 'H2.00-3.00XL') are returned unchanged."""
    m = RANGE_RE.match(token)
    if not m:
        return [token]
    letters, lo_s, hi_s, suffix = m.groups()
    lo, hi = int(lo_s), int(hi_s)
    if hi <= lo:
        return [token]
    step = 10 if (hi - lo) >= 10 else 5
    out = []
    n = lo
    while n < hi:
        out.append(f"{letters}{n}{suffix}")
        n += step
    out.append(f"{letters}{hi}{suffix}")
    return out


def resolve_models(db: Session, prefix_row: SerialPrefix, model_index: dict | None = None) -> list[Forklift]:
    """Match a prefix row's model-family text against the catalog. A prefix names
    a family, not a specific truck in our DB -- this is a query-time lookup, no FK."""
    if model_index is None:
        model_index = _model_index(db)
    ids: list[int] = []
    for token in prefix_row.models:
        for variant in _expand(token):
            fid = model_index.get((prefix_row.brand, _NORM(variant)))
            if fid is not None and fid not in ids:
                ids.append(fid)
    if not ids:
        return []
    by_id = {f.id: f for f in db.query(Forklift).filter(Forklift.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


def _prefix_rows(db: Session, prefix: str) -> list[SerialPrefix]:
    return db.query(SerialPrefix).filter(SerialPrefix.prefix == prefix).all()


def _family_candidates(db: Session, digits: str) -> list[SerialPrefix]:
    return db.query(SerialPrefix).filter(SerialPrefix.prefix.like(f"_{digits}")).all()


def _build_from_rows(db: Session, prefix: str, rows: list[SerialPrefix]) -> SerialDecodeResult:
    model_index = _model_index(db)
    families: list[str] = []
    matches: list[SerialMatch] = []
    seen_ids: set[int] = set()
    twin_prefixes: set[str] = set()
    for row in rows:
        families.append(", ".join(row.models))
        for f in resolve_models(db, row, model_index):
            if f.id not in seen_ids:
                seen_ids.add(f.id)
                matches.append(SerialMatch(id=f.id, manufacturer=f.manufacturer, model=f.model))
        if row.twin_prefix:
            twin_prefixes.add(row.twin_prefix)

    twin_families: list[str] = []
    for tp in sorted(twin_prefixes):
        for row in _prefix_rows(db, tp):
            twin_families.append(f"{tp}: {', '.join(row.models)}")

    brands = {row.brand for row in rows}
    return SerialDecodeResult(
        kind="prefix",
        brand=brands.pop() if len(brands) == 1 else None,
        prefix=prefix,
        generation=prefix[0],
        family_digits=prefix[1:],
        families=families,
        matches=matches,
        twin_prefix=", ".join(sorted(twin_prefixes)) if twin_prefixes else None,
        twin_families=twin_families,
    )


def _decode_prefix(db: Session, prefix: str) -> SerialDecodeResult:
    rows = _prefix_rows(db, prefix)
    if rows:
        result = _build_from_rows(db, prefix, rows)
        if len(rows) > 1:
            result.notes.append(
                f"{len(rows)} source rows matched prefix {prefix} (regional/sub-model variants) — all are shown."
            )
        return result

    digits = prefix[1:]
    candidates = _family_candidates(db, digits)
    if candidates:
        result = _build_from_rows(db, prefix, candidates)
        result.kind = "unknown_prefix"
        result.family_guess = True
        gens = sorted({c.prefix[0] for c in candidates})
        result.notes.append(
            f"Prefix {prefix} isn't in the source tables. Digits {digits} match known "
            f"family/families under generation letter(s) {', '.join(gens)} — probably a "
            f"{prefix[0]}-generation version of one of these. Unconfirmed."
        )
        return result

    return SerialDecodeResult(
        kind="unknown_prefix",
        prefix=prefix,
        generation=prefix[0],
        family_digits=digits,
        notes=[f"Prefix {prefix} was not found in the source tables, and no family shares digits {digits}."],
    )


def _decode_serial(db: Session, s: str) -> SerialDecodeResult:
    m = SERIAL_RE.match(s)
    prefix, plant, seq, year_code = m.group("prefix"), m.group("plant"), m.group("seq"), m.group("year")

    result = _decode_prefix(db, prefix)
    result.kind = "serial"
    result.plant = plant
    result.sequence = seq
    result.year_code = year_code

    plant_row = db.get(SerialPlantCode, plant)
    result.plant_location = plant_row.location if plant_row else None

    year_row = db.get(SerialYearCode, year_code)
    candidates = list(year_row.years) if year_row else []
    # The letter cycle runs into the future; a built truck cannot postdate today.
    candidates = [c for c in candidates if c <= date.today().year] or candidates

    if result.matches and candidates:
        this_year = date.today().year
        narrowed = []
        for cand in candidates:
            for sm in result.matches:
                f = db.get(Forklift, sm.id)
                ys, ye = f.year_start, (f.year_end or this_year)
                if ys is None or (ys <= cand <= ye):
                    narrowed.append(cand)
                    break
        if narrowed and len(narrowed) < len(candidates):
            ruled_out = [c for c in candidates if c not in narrowed]
            result.notes.append(
                f"Ruled out {', '.join(map(str, sorted(ruled_out, reverse=True)))} based on the "
                "matched model's known production years."
            )
            candidates = narrowed

    result.candidate_years = sorted(set(candidates), reverse=True)
    if any(y >= 2017 for y in candidates):
        result.correction_note = CORRECTION_NOTE
    return result


def _decode_pre1995(db: Session, s: str) -> SerialDecodeResult:
    notes: list[str] = []
    candidate_years: list[int] = []

    if re.match(r"^[0-9]{6}$", s):
        digits = [int(c) for c in s]
        rider_yy = digits[0] * 10 + digits[1]
        works_yy = digits[1] * 10 + digits[2]
        for yy, rule in ((rider_yy, "rider/electric truck (chars 1-2 = year)"),
                         (works_yy, "worksaver/warehouse truck (chars 2-3 = year)")):
            if 40 <= yy <= 57:
                y = 1900 + yy
                notes.append(f"If a {rule}: {y}.")
                if y not in candidate_years:
                    candidate_years.append(y)
    else:
        m = re.match(r"^(H|J|A[B-K])", s)
        if m:
            key = m.group(1)
            row = next((r for r in _PRE1995 if r["era"] == "1958-1968" and r["key"] == key), None)
            if row:
                candidate_years = [int(row["value"])]
                notes.append(f"Alpha year-prefix {key} (1958-1968 era).")

    notes.append(
        "Yale used five different numbering systems before Q3 1995 — this is a reference "
        "explainer only, no attempt is made to match it against the forklift catalog."
    )
    return SerialDecodeResult(
        kind="yale_pre1995",
        candidate_years=sorted(set(candidate_years), reverse=True),
        pre1995_eras=_PRE1995,
        notes=notes,
    )


def decode(db: Session, raw: str) -> SerialDecodeResult:
    """Entry point: raw user input -> SerialDecodeResult, or None if unrecognized."""
    kind = classify(raw)
    s = normalize(raw)
    if kind == "serial":
        return _decode_serial(db, s)
    if kind == "prefix":
        return _decode_prefix(db, s)
    if kind == "yale_pre1995":
        return _decode_pre1995(db, s)
    return SerialDecodeResult(
        kind="unrecognized",
        notes=["Not recognized as a Hyster/Yale OEM serial number, 4-character prefix, or pre-1995 Yale serial."],
    )
