"""Migrate the kits' `Fits:` lines into kit_fitment.

Dry run by default; `--apply` writes. Idempotent -- re-running adds nothing,
because every row is matched against what is already there before insert.

WHAT THIS DOES NOT DO
---------------------
It does not expand a range into one row per truck. `GC35-50K` is stored as
written, and app/kit_ranges.py reads it at query time. Expanding here would bake
one reading of the notes into the data permanently, and the notes are the part
most likely to be corrected.

It does not touch `kit_forklift`. kit_fitment is additive and runs alongside it
until the new path is proven (see the plan).

It does not invent a serial condition. 6 of the 119 kits mention a serial and
each is read by hand below, because there is no grammar to generalise from six
examples: one is a numeric range, two name a documented serial prefix, and three
say a serial test exists without saying what it is. A kit with nothing on record
gets `always` -- the sheet's own claim, no more.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import kit_ranges  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Kit, KitFitment  # noqa: E402


@dataclass(frozen=True)
class Cond:
    """A serial condition to stamp on the rows it applies to."""
    condition: str = "always"
    lo: int | None = None
    hi: int | None = None
    scope: str = ""
    pattern: str | None = None
    note: str = ""


# Rules as (kit_id, manufacturer or None for any, model_expr or None for any,
# Cond). The most specific match wins, so a kit-wide reading can be overridden
# for one brand or one model expression.
#
# Kit 2  -- `(serial # 10231+)` sits at the end of the whole Fits: line, so it
#           binds every model on it. Open-ended high bound. CAT/Mitsubishi runs
#           one plain sequence, which is why app/serials.py lists it as ordered.
# Kit 24 -- 'within the serial # if there is "3"'. A character test, not a
#           range. Nothing here can evaluate it, so it is held verbatim and
#           answers "cannot say" rather than a quiet match.
# Kit 81 -- 'Series R177, within the serial # of the truck'. R177 IS a
#           documented Hyster prefix (data/reference/hy_serial_prefixes.json:
#           R177 = H40-70FT, 4000-7000 lbs, 4-wheel pneumatic), so this is a
#           scope, not a range: the kit fits when the truck's serial carries
#           that prefix. Its recorded twin is E875 = Yale GP040-070VX, same
#           capacity and same tire -- so the Yale GP row gets E875. The kit's
#           other Yale rows (GC/GLC = cushion, GLP/GDP) are NOT covered by
#           E875, which is pneumatic-only, so they keep the caveat verbatim
#           instead of borrowing a prefix that does not name them.
# Kit 89 -- 'Serial # defined' states that a constraint exists without stating
#           it. `always` would claim the kit fits every serial, which the note
#           specifically denies, so this is a pattern a person must resolve.
# Kit 100/101 -- Linde series numbers (1276, 1346) read out of the serial.
#           app/serials.py has no scheme for Linde, so even a real bound here
#           could not be compared. Verbatim.
_CAT = "CAT/Mitsubishi"
_SERIAL_RULES: list[tuple[int, str | None, str | None, Cond]] = [
    (2, None, None, Cond("range", lo=10231, hi=None,
                         note="kit sheet: (serial # 10231+)")),
    (24, None, None, Cond("pattern",
                          pattern='within the serial # if there is "3" - 4NR3 the "3" = 36v',
                          note="character test, not a range")),
    (81, "Hyster", None, Cond("range", scope="R177",
                              note="Caveats: Series R177, within the serial # of the truck")),
    (81, "Yale", None, Cond("pattern",
                            pattern="Series R177 (Hyster) in the truck serial; Yale equivalent not established for this model",
                            note="E875 is the recorded twin of R177 but names GP040-070VX only")),
    (81, "Yale", "GP040-070VX", Cond("range", scope="E875",
                                     note="hy_serial_prefixes.json: E875 = Yale GP040-070VX, twin of R177")),
    (89, None, None, Cond("pattern", pattern="Serial # defined (see spec sheet)",
                          note="Caveats say a serial constraint exists but do not state it")),
    (100, None, None, Cond("pattern", pattern="1276 series, read from the serial #",
                           note="no serial scheme on file for Linde")),
    (101, None, None, Cond("pattern", pattern="Series 1346, looking in the serial #",
                           note="no serial scheme on file for Linde; caveat also "
                                "narrows the model to E18S-E20S, which the Fits: line "
                                "does not say -- unresolved")),
]

# Kits whose `Fits:` line is missing from the notes field but is on the sheet.
#
# 174-MCA1-00G05-01 is the half of the MCA1/MCA2 pair that makes the serial
# split meaningful, and its notes field holds the string "test link" -- the
# fitment was never entered. The line below is the sheet's, quoted in chat on
# 2026-10-27. It is marked `documented` with that provenance in source_note, so
# it is distinguishable from anything the parser read. The kits.notes field is
# deliberately NOT rewritten here: this script does not edit the catalog.
_SHEET_FITS: dict[int, tuple[str, list[str], Cond, str]] = {
    1: (
        _CAT,
        ["GC35-70K", "GP40-50K", "FGC35-70K", "FG40-50K"],
        Cond("range", lo=1, hi=10230, note="kit sheet: (serial # 00001-10230)"),
        "kit sheet, quoted in chat 2026-10-27; the kits.notes field says only "
        "'test link', so the parser reads nothing from it",
    ),
}


def _condition_for(kit_id: int, oem: str, model_expr: str) -> Cond:
    """Most specific matching rule, else an unconstrained `always`."""
    best: tuple[int, Cond] | None = None
    for rule_kit, rule_oem, rule_expr, cond in _SERIAL_RULES:
        if rule_kit != kit_id:
            continue
        if rule_oem is not None and rule_oem != oem:
            continue
        if rule_expr is not None and rule_expr != model_expr:
            continue
        rank = (rule_oem is not None) + (rule_expr is not None)
        if best is None or rank > best[0]:
            best = (rank, cond)
    return best[1] if best else Cond()


def _is_part_number(token: str) -> bool:
    """Kit 45's notes name another KIT in the Fits: line ('Raymond
    174-R750-00G05'). The parser files it as a foreign-brand model; it is a part
    number and must not become a fitment row."""
    return "174-" in token.upper()


def _planned(db) -> list[dict]:
    """Every row this migration would write, in kit order."""
    rows: list[dict] = []
    seen: set[tuple[int, str, str]] = set()

    def add(kit: Kit, oem: str, model_expr: str, source: str, note_extra: str = "") -> None:
        key = (kit.id, oem, model_expr)
        if key in seen:
            return          # the notes repeat themselves; one row per truck
        seen.add(key)
        cond = _condition_for(kit.id, oem, model_expr)
        note = f"{source}: {model_expr}"
        if cond.note:
            note += f" | serial: {cond.note}"
        if note_extra:
            note += f" | {note_extra}"
        rows.append({
            "kit_id": kit.id, "sku": kit.sku, "manufacturer": oem,
            "model_expr": model_expr, "condition": cond.condition,
            "serial_lo": cond.lo, "serial_hi": cond.hi,
            "serial_scope": cond.scope, "serial_pattern": cond.pattern,
            "source_note": note,
        })

    for kit in db.query(Kit).order_by(Kit.id).all():
        if kit.id in _SHEET_FITS:
            oem, exprs, cond, provenance = _SHEET_FITS[kit.id]
            for expr in exprs:
                key = (kit.id, oem, expr)
                if key in seen:
                    continue
                seen.add(key)
                note = f"sheet Fits: {expr}"
                if cond.note:
                    note += f" | serial: {cond.note}"
                rows.append({
                    "kit_id": kit.id, "sku": kit.sku, "manufacturer": oem,
                    "model_expr": expr, "condition": cond.condition,
                    "serial_lo": cond.lo, "serial_hi": cond.hi,
                    "serial_scope": cond.scope, "serial_pattern": cond.pattern,
                    "source_note": f"{note} | {provenance}",
                })
            continue

        fits = kit_ranges.parse_notes(kit.notes, kit.id, kit.sku)
        if not fits.oem:
            continue
        for code in fits.exact:
            add(kit, fits.oem, code, "Fits")
        for rng in fits.ranges:
            add(kit, fits.oem, rng.source, "Fits range")
        for twin_oem, token in fits.foreign:
            if _is_part_number(token):
                continue
            add(kit, twin_oem, kit_ranges.normalise(token), "Fits (twin named inline)")
    return rows


def _existing_key(row: KitFitment) -> tuple:
    return (row.kit_id, row.manufacturer, row.model_expr, row.condition,
            row.serial_lo, row.serial_hi, row.serial_scope or "",
            row.serial_pattern)


def _planned_key(p: dict) -> tuple:
    return (p["kit_id"], p["manufacturer"], p["model_expr"], p["condition"],
            p["serial_lo"], p["serial_hi"], p["serial_scope"] or "",
            p["serial_pattern"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the rows")
    ap.add_argument("--confirmed-by", default="migrate_kit_fitment",
                    help="who to credit on the documented rows")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        plan = _planned(db)
        have = {_existing_key(r) for r in db.query(KitFitment).all()}
        new = [p for p in plan if _planned_key(p) not in have]

        kits = db.query(Kit).order_by(Kit.id).all()
        unreadable = [
            k for k in kits
            if k.id not in _SHEET_FITS
            and not kit_ranges.parse_notes(k.notes, k.id, k.sku).readable
        ]

        by_cond: dict[str, int] = {}
        for p in plan:
            by_cond[p["condition"]] = by_cond.get(p["condition"], 0) + 1

        print(f"kits:            {len(kits)}")
        print(f"rows planned:    {len(plan)}")
        print(f"already present: {len(plan) - len(new)}")
        print(f"to insert:       {len(new)}")
        print("by serial condition: " + ", ".join(
            f"{k}={v}" for k, v in sorted(by_cond.items())))
        print(f"kits the parser cannot read: {len(unreadable)}")
        for k in unreadable:
            why = kit_ranges.parse_notes(k.notes, k.id, k.sku).unparsed
            print(f"    #{k.id:<4} {k.sku:<24} {why}")

        print("\nrows carrying a serial condition:")
        for p in plan:
            if p["condition"] == "always":
                continue
            print(f"    #{p['kit_id']:<4} {p['sku']:<22} {p['manufacturer']:<16} "
                  f"{p['model_expr']:<16} {p['condition']:<8} "
                  f"lo={p['serial_lo']} hi={p['serial_hi']} scope={p['serial_scope']!r}")

        if not args.apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply.")
            return 0

        now = datetime.now(timezone.utc)
        for p in new:
            db.add(KitFitment(
                kit_id=p["kit_id"], manufacturer=p["manufacturer"],
                model_expr=p["model_expr"], condition=p["condition"],
                serial_lo=p["serial_lo"], serial_hi=p["serial_hi"],
                serial_scope=p["serial_scope"], serial_pattern=p["serial_pattern"],
                origin="documented", source_note=p["source_note"],
                confirmed_by=args.confirmed_by, confirmed_at=now,
            ))
        db.commit()
        print(f"\ninserted {len(new)} kit_fitment rows "
              f"(total now {db.query(KitFitment).count()}).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
