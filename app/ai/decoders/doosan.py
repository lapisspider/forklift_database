"""Offline decoding of Doosan/Bobcat serial numbers (1993-2010 reference data).

Shape: 2-char prefix ([A-Z]{2} | [A-Z][0-9] | [0-9]{2}) + trailing dash +
4-5 digit sequence, e.g. L7-00116, FH-00545, 21-00010.

Owner rulings (see data/reference/doosan_*.json and the task brief):
  1. Sequences are batch-allocated, NOT a calendar counter -- a higher
     sequence can predate a lower one. See estimate_year().
  2. The model's leading letter is a chassis family, not the fuel
     (D15S-5LP on NK- is an LP build on the diesel chassis) -- never
     inferred here; power/fuel_trans are rendered verbatim from the
     prefix row only.
"""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ...models import DoosanPrefix, DoosanYearSerial, Forklift
from ...schemas import DoosanPrefixRow, SerialDecodeResult, SerialMatch
from .types import Candidate

name = "doosan"
manufacturers = ("Bobcat/Doosan",)

_PREFIX_SHAPE = r"(?:[A-Z]{2}|[A-Z][0-9]|[0-9]{2})"
SERIAL_RE = re.compile(rf"^(?P<prefix>{_PREFIX_SHAPE})(?P<seq>[0-9]{{4,5}})$")
PREFIX_RE = re.compile(rf"^{_PREFIX_SHAPE}$")

CHASSIS_NOTE = (
    "The leading letter is a chassis family, not the fuel — e.g. D15S-5LP on "
    "NK- is an LP build on the diesel chassis."
)

# Brake/drive codes we've seen tacked onto a reference model that the catalog
# doesn't carry (D35S-5SB -> D35S-5). Longest first so e.g. "0DB2" is tried
# before "0DB". A leading digit immediately before the code (the generation,
# e.g. the "5" in "-5SB") is always kept; only the code itself is dropped.
_BRAKE_CODES = ["0DB2", "0DB3", "SB2", "SB3", "0DB", "SB", "ODB"]


def _strip_brake_suffix(model: str) -> str | None:
    for code in _BRAKE_CODES:
        if model.endswith(code):
            rest = model[: -len(code)]
            if re.search(r"-\d$", rest):
                return rest
            return rest[:-1] if rest.endswith("-") else rest
    return None


def _expand_segment(seg: str) -> list[str]:
    """'G15S/18S-5' -> ['G15S-5', 'G18S-5']. Non-range segments (including
    'B13T-2 / PLUS', which isn't a numeric range) come back unchanged."""
    if "/" not in seg:
        return [seg]
    m = re.match(r"^([A-Za-z]+)(.*)$", seg)
    if not m:
        return [seg]
    prefix, rest = m.groups()
    tokens = rest.split("/")
    if len(tokens) < 2:
        return [seg]
    last = tokens[-1]
    lm = re.match(r"^(\d+)([A-Za-z]*)(-.+)?$", last)
    if not lm:
        return [seg]
    last_digits, last_letters, last_dash = lm.groups()
    last_dash = last_dash or ""
    out = []
    for i, tok in enumerate(tokens):
        if i == len(tokens) - 1:
            out.append(prefix + tok)
            continue
        tm = re.match(r"^(\d+)([A-Za-z]*)$", tok)
        if not tm:
            return [seg]
        tok_digits, tok_letters = tm.groups()
        suffix = last_dash if tok_letters else (last_letters + last_dash)
        out.append(prefix + tok_digits + tok_letters + suffix)
    return out


def expand_model(raw: str) -> list[str]:
    """'G15S/18S-5 & G20SC-5' -> ['G15S-5', 'G18S-5', 'G20SC-5']."""
    out: list[str] = []
    for seg in re.split(r"[,&]", raw):
        out.extend(_expand_segment(seg.strip()))
    return out


def _prefix_rows(db: Session, prefix: str) -> list[DoosanPrefix]:
    return db.query(DoosanPrefix).filter(DoosanPrefix.prefix == prefix).all()


def _year_rows(db: Session, prefix: str) -> list[DoosanYearSerial]:
    return (
        db.query(DoosanYearSerial)
        .filter(DoosanYearSerial.prefix == prefix)
        .order_by(DoosanYearSerial.year)
        .all()
    )


def resolve_catalog(db: Session, model_families: list[str]) -> list[Forklift]:
    """Match reference model-family text against the Bobcat/Doosan catalog:
    exact first, then with a brake/drive suffix stripped (owner-approved)."""
    catalog = {
        f.model: f
        for f in db.query(Forklift).filter(Forklift.manufacturer == "Bobcat/Doosan").all()
    }
    found: list[Forklift] = []
    seen: set[int] = set()
    for fam in model_families:
        for cand in (fam, _strip_brake_suffix(fam)):
            if not cand:
                continue
            fk = catalog.get(cand)
            if fk and fk.id not in seen:
                seen.add(fk.id)
                found.append(fk)
    return found


def estimate_year(db: Session, prefix: str, seq: int) -> dict:
    """Owner ruling: Doosan sequences are batch-allocated, not a calendar
    counter -- a higher sequence can predate a lower one. Returns
    {'note': str|None, 'flags': list[str]}."""
    markers = _year_rows(db, prefix)
    if not markers:
        return {"note": None, "flags": []}

    if len(markers) == 1:
        m = markers[0]
        return {
            "note": f"Only one batch marker on file ({m.year}) — can't bracket a year from a single point.",
            "flags": [m.flag] if m.flag else [],
            "basis": "single_marker", "years": [m.year],
        }

    earliest = markers[0]  # sorted by year ascending
    if seq < earliest.sequence:
        return {
            "note": (
                f"At or before {earliest.year} — earlier than the earliest batch marker on file "
                f"for this prefix ({earliest.raw}, {earliest.year})."
            ),
            "flags": [earliest.flag] if earliest.flag else [],
            "basis": "at_or_before", "years": [earliest.year],
        }

    candidates = [m for m in markers if m.sequence <= seq] or [earliest]
    chosen = max(candidates, key=lambda m: m.sequence)
    seqs_by_year = [m.sequence for m in markers]
    monotonic = all(seqs_by_year[i] <= seqs_by_year[i + 1] for i in range(len(seqs_by_year) - 1))

    if not monotonic:
        yrs = sorted({m.year for m in candidates})
        span = str(yrs[0]) if yrs[0] == yrs[-1] else f"{yrs[0]}–{yrs[-1]}"
        note = (
            f"This prefix's batch markers aren't monotonic (sequence doesn't track cleanly with "
            f"year) — best estimate is a range: {span}. Closest listed marker: {chosen.raw} "
            f"({chosen.year}). Doosan sequences are batch-allocated, not calendar counters, so a "
            "higher number can predate a lower one. Treat this as a hint, not a build date — "
            "verify on the data plate."
        )
        return {"note": note, "flags": [m.flag for m in candidates if m.flag],
                "basis": "range", "years": yrs}

    note = (
        f"Approximate year: {chosen.year} — closest listed batch start ({chosen.raw}, {chosen.year}). "
        "Doosan sequences are batch-allocated, not calendar counters, so a higher number can predate "
        "a lower one. Treat this as a hint, not a build date — verify on the data plate."
    )
    return {"note": note, "flags": [chosen.flag] if chosen.flag else [],
            "basis": "approximate", "years": [chosen.year]}


def _decode_prefix_only(db: Session, prefix: str) -> SerialDecodeResult:
    prow = _prefix_rows(db, prefix)
    yrow = _year_rows(db, prefix)
    orphan = not prow and bool(yrow)

    families: list[str] = []
    doosan_rows: list[DoosanPrefixRow] = []
    model_families: list[str] = []

    if prow:
        for r in prow:
            families.append(r.model)
            model_families.extend(expand_model(r.model))
            doosan_rows.append(DoosanPrefixRow(
                model=r.model, power=r.power, engine=r.engine, certification=r.certification,
                fuel_trans=r.fuel_trans, brake=r.brake, voltage=r.voltage, system=r.system,
                configuration=r.configuration,
            ))
    elif yrow:
        families = sorted({r.model for r in yrow})
        model_families = list(families)

    matches = resolve_catalog(db, model_families)
    notes: list[str] = []
    kind = "doosan_prefix"
    if not prow and not yrow:
        kind = "doosan_unknown_prefix"
        notes.append(f"Prefix {prefix} was not found in the Doosan source tables.")
    elif orphan:
        notes.append(
            f"{prefix} isn't in the prefix table -- the family comes from the year table only, "
            "with no engine/brake/voltage data."
        )
    if len(prow) > 1:
        notes.append(f"{len(prow)} source rows matched prefix {prefix} -- all are shown.")

    return SerialDecodeResult(
        kind=kind,
        prefix=prefix,
        families=families,
        matches=[SerialMatch(id=f.id, manufacturer=f.manufacturer, model=f.model) for f in matches],
        doosan_rows=doosan_rows,
        orphan=orphan,
        chassis_note=CHASSIS_NOTE if doosan_rows else None,
        notes=notes,
    )


def _decode_serial(db: Session, m: re.Match) -> SerialDecodeResult:
    prefix = f"{m.group('prefix')}-"
    seq_raw = m.group("seq")
    seq = int(seq_raw)

    result = _decode_prefix_only(db, prefix)
    result.kind = "doosan_serial"
    result.sequence = seq_raw

    est = estimate_year(db, prefix, seq)
    result.year_note = est["note"]
    result.year_basis = est.get("basis")
    result.year_values = est.get("years", [])
    result.anomalies = [f"Source anomaly: {f}" for f in est["flags"] if f]
    return result


def decode(db: Session, norm: str) -> SerialDecodeResult:
    m = SERIAL_RE.match(norm)
    if m:
        return _decode_serial(db, m)
    if PREFIX_RE.match(norm):
        return _decode_prefix_only(db, f"{norm}-")
    return SerialDecodeResult(
        kind="unrecognized",
        notes=["Not recognized as a Doosan/Bobcat serial number or prefix."],
    )


def match(db: Session, norm: str) -> Candidate | None:
    m = SERIAL_RE.match(norm)
    if m:
        prefix = f"{m.group('prefix')}-"
        if _prefix_rows(db, prefix):
            return Candidate(name, "doosan_serial", 1.00, f"full Doosan serial shape, prefix {prefix} on file")
        if _year_rows(db, prefix):
            return Candidate(name, "doosan_serial", 0.80,
                              f"full Doosan serial shape, prefix {prefix} known only from the year table")
        return Candidate(name, "doosan_serial", 0.35, f"full Doosan serial shape, prefix {prefix} unknown")

    if PREFIX_RE.match(norm):
        prefix = f"{norm}-"
        if _prefix_rows(db, prefix) or _year_rows(db, prefix):
            return Candidate(name, "doosan_prefix", 0.60, f"bare prefix {prefix} on file")
        return None  # bare 2-char shape with nothing behind it is too generic to claim

    return None


class _DoosanDecoder:
    name = name
    manufacturers = manufacturers
    match = staticmethod(match)
    decode = staticmethod(decode)


DECODER = _DoosanDecoder()
