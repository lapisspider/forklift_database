"""Fill series gaps the kits document, one web-confirmed model at a time.

DRY RUN BY DEFAULT, like `fix_truck_class.py`. Pass `--apply` to write.

`audit_kit_ranges.py` lists the designations a kit's `Fits:` line names that
have no catalog row. This script confirms them against the web through the
existing pipeline (`lookup.lookup(..., mode="model", web=True)`) and inserts
only the ones that come back complete. Nothing here derives a row from code
arithmetic: a designation inside a documented range is a CANDIDATE, and the
OEM's own published sheet is what turns it into a row.

A candidate is accepted only when all of this holds:

  * the lookup found it, and the model it returned IS the model asked for.
    The web happily answers a near miss -- CLAUDE.md records an H70D sheet
    coming back for an H50D -- so a returned designation that does not match
    is a rejection, not a correction.
  * the manufacturer it returned is the kit's OEM.
  * every field is filled: series, capacity, fuel type, class and years.
  * there is a spec-sheet PDF that NAMES THIS MODEL, and the link check
    reached it. A family-level sheet is not enough: it covers a span of
    trucks, and the extractor then has to pick one capacity out of several.
    That is exactly how `7FGU80` came back rated 7938 kg (17,500 lb) for an
    8,000 lb truck.
  * the capacity sits between the capacities of its neighbours already on
    file in the same family. A new row that is not in order with the siblings
    around it is wrong, whatever the sheet said.

Each accepted row is linked to the kit whose documented range names it, and to
nothing else -- a kit fits what its own notes say it fits.

Costs money: one spec-sheet search plus extraction per candidate. The tranche
is therefore explicit rather than "everything in the report".

Run:
    .venv/Scripts/python.exe fill_kit_gaps.py                  # dry run, approved tranche
    .venv/Scripts/python.exe fill_kit_gaps.py --apply
    .venv/Scripts/python.exe fill_kit_gaps.py --models "Toyota 8FGCU25" --apply
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai import lookup as L  # noqa: E402
from app.ai.oem_alias import split_oem  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.kit_ranges import _decompose, normalise, parse_all  # noqa: E402
from app.models import Forklift, Kit, KitForklift  # noqa: E402
from app.schemas import clean_series  # noqa: E402

# The tranche the user approved: the Toyota 8-series holes that started this
# work, the 7-series endpoints the notes write out, and the CAT candidates
# backed by the most sibling model families. Ordered so the Toyota rows are
# confirmed first.
TRANCHE: list[str] = [
    # Toyota 8-series interior -- 8FGCU/8FGU/8FDU 20 and 32 are on file, 25
    # and 30 are the two highest-volume sizes and are missing.
    "Toyota 8FGCU25", "Toyota 8FGCU30",
    "Toyota 8FGU25", "Toyota 8FGU30",
    "Toyota 8FDU25", "Toyota 8FDU30",
    # Toyota 7-series endpoints, written out in kits 34 and 36. The catalog
    # holds `7FDU35-80` and `7FGU35-80` as single rows -- a range stored as a
    # model -- so the real endpoints have no row of their own.
    "Toyota 7FDU35", "Toyota 7FDU80",
    "Toyota 7FGU35", "Toyota 7FGU80",
    "Toyota 7FGCU45", "Toyota 7FGCU70",
    # CAT/Mitsubishi: the K5 generation kit 4 names outright, then the
    # interior candidates with the widest support in CAT's own numbering.
    "CAT GC35K5", "CAT GC40K5", "CAT GC50K5",
    "CAT GP40K5", "CAT GP50K5",
    "CAT FG25N", "CAT FG30N",
    "CAT FGC25N", "CAT FGC30N",
    "CAT EC25N", "CAT FBC25LN",
    "CAT GP50N1", "CAT FG50N1",
]

REQUIRED = ("series", "capacity_kg", "fuel_type", "truck_class", "year_start")


def _flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        return argv[argv.index(name) + 1]
    return None


def kits_naming(designation: str, oem: str, fits) -> list[int]:
    """The kits whose own notes name this designation, and no others."""
    want = normalise(designation)
    out = []
    for kit in fits:
        if kit.oem != oem:
            continue
        if want in {normalise(e) for e in kit.exact}:
            out.append(kit.kit_id)
            continue
        if any(rng.contains(want) for rng in kit.ranges):
            out.append(kit.kit_id)
    return out


def reject_reason(query: str, result) -> str | None:
    """Why this candidate must not become a row, or None to accept it."""
    oem, rest = split_oem(query)
    if result.already_in_db:
        return "already in the catalog"
    if not result.found:
        return (result.message or "not found online")[:88]
    specs = result.specs
    if specs is None:
        return "found, but returned no specs"
    if normalise(specs.model or "") != normalise(rest):
        return f"returned a different model: {specs.model!r}"
    # The extractor echoes the OEM as the sheet words it ("CAT (Mitsubishi
    # Caterpillar Forklift America)"), so it is compared through the alias
    # table rather than by string equality -- but it still has to resolve to
    # the OEM that was asked for.
    returned_oem, _ = split_oem(specs.manufacturer or "")
    if returned_oem and returned_oem != oem:
        return f"returned another brand: {specs.manufacturer!r} reads as {returned_oem}"
    missing = [f for f in REQUIRED if getattr(specs, f, None) in (None, "")]
    if missing:
        return "fields missing: " + ", ".join(missing)
    if not result.pdf_url:
        return "no spec-sheet PDF"
    if result.pdf_status and result.pdf_status >= 400:
        return f"spec-sheet PDF returned HTTP {result.pdf_status}"
    if "names this model" not in (result.pdf_note or ""):
        return "only a family-level PDF, none naming this model"
    return None


def capacity_out_of_order(db, oem: str, model: str, capacity: float) -> str | None:
    """Why this capacity disagrees with the siblings already on file.

    A family's capacity rises with its model number, so a new row has to sit
    between its neighbours. This is the same test `audit_kit_ranges.py` section
    0 runs over the catalog, pointed at a candidate instead: the web returned
    `7FGCU70` at 3175 kg with `7FGCU35` already on file at 3600, and no sheet
    makes a 70 lighter than a 35.
    """
    want = _decompose(normalise(model))
    if not want:
        return None
    prefix, number, suffix, _width = want[-1]
    lower = upper = None
    for row_model, row_cap in db.query(Forklift.model, Forklift.capacity_kg).filter(
            Forklift.manufacturer == oem).all():
        if row_cap is None:
            continue
        for p, n, s, _w in _decompose(normalise(row_model or "")):
            if p == prefix and s == suffix and n != number:
                if n < number and (lower is None or n > lower[0]):
                    lower = (n, row_cap, row_model)
                if n > number and (upper is None or n < upper[0]):
                    upper = (n, row_cap, row_model)
                break
    if lower and capacity < lower[1]:
        return f"{capacity:.0f} kg is below {lower[2]} at {lower[1]:.0f} kg"
    if upper and capacity > upper[1]:
        return f"{capacity:.0f} kg is above {upper[2]} at {upper[1]:.0f} kg"
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    picked = _flag(sys.argv, "--models")
    queries = [q.strip() for q in picked.split(",")] if picked else list(TRANCHE)

    init_db()
    db = SessionLocal()
    try:
        kit_rows = db.query(Kit.id, Kit.sku, Kit.notes).order_by(Kit.id).all()
        fits = parse_all(kit_rows)

        print("=" * 78)
        print(f"FILL KIT GAPS -- {len(queries)} candidates, "
              f"{'APPLYING' if apply else 'DRY RUN (pass --apply to write)'}")
        print("=" * 78)
        print("Each candidate costs one spec-sheet search plus extraction.\n")

        added: list[tuple[str, list[int]]] = []
        rejected: list[tuple[str, str]] = []

        for query in queries:
            result = L.lookup(db, query, mode="model", web=True)
            why = reject_reason(query, result)
            if why:
                rejected.append((query, why))
                print(f"  skip  {query:24s} {why}")
                continue

            specs = result.specs
            oem, _ = split_oem(query)
            disorder = capacity_out_of_order(db, oem, specs.model or "", specs.capacity_kg)
            if disorder:
                rejected.append((query, "out of order with its siblings: " + disorder))
                print(f"  skip  {query:24s} out of order with its siblings: {disorder}")
                continue
            linked = kits_naming(specs.model or "", oem, fits)
            if not linked:
                rejected.append((query, f"confirmed, but no kit's notes name it under {oem!r}"))
                print(f"  skip  {query:24s} confirmed, but no kit names it under {oem!r}")
                continue

            exact_pdf = "names this model" in (result.pdf_note or "")
            print(f"  ADD   {specs.model:24s} {oem:16s} {specs.capacity_kg:>6.0f} kg  "
                  f"{specs.fuel_type:14s} {specs.truck_class:10s} "
                  f"series={specs.series!r} pdf={'exact' if exact_pdf else 'family'} "
                  f"-> kit {','.join(str(k) for k in linked)}")
            added.append((specs.model or query, linked))

            if not apply:
                continue

            forklift = Forklift(
                manufacturer=oem,
                series=clean_series(specs.series),
                model=specs.model,
                year_start=specs.year_start,
                year_end=specs.year_end,
                capacity_kg=specs.capacity_kg,
                fuel_type=specs.fuel_type,
                chassis=specs.chassis,
                truck_class=specs.truck_class,
                source_url=result.source_url,
                pdf_url=result.pdf_url,
                notes=specs.notes,
                fetched_at=datetime.now(timezone.utc),
            )
            db.add(forklift)
            db.flush()
            if not forklift.internal_serial:
                forklift.internal_serial = _next_serial(db)
            for kit_id in linked:
                db.add(KitForklift(kit_id=kit_id, forklift_id=forklift.id, tier="bronze"))
            db.commit()

        print(f"\n{'-' * 78}")
        print(f"{len(added)} confirmed, {len(rejected)} skipped.")
        if not apply and added:
            print("Nothing was written -- pass --apply.")
        if rejected:
            print("\nSkipped, with the reason the web gave:")
            for query, why in rejected:
                print(f"  {query:24s} {why}")
        if apply and added:
            print("\nCatalog data changed: refresh deploy/seed.db per CLAUDE.md.")
            for model, linked in added:
                print(f"  wrote {model:24s} linked to kit {','.join(str(k) for k in linked)}")
        return 0
    finally:
        db.close()


def _next_serial(db) -> str:
    """Same rule as the app: one past the high-water mark, never reused."""
    import re

    top = 0
    for (serial,) in db.query(Forklift.internal_serial).all():
        match = re.fullmatch(r"FLT-(\d+)", serial or "")
        if match:
            top = max(top, int(match.group(1)))
    return f"FLT-{top + 1:04d}"


if __name__ == "__main__":
    raise SystemExit(main())
