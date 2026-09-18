"""Import Hyster's 3rd-generation narrow-aisle reach truck lineup (ZR3/ZDR3/ZRS3/ZDRS3).

Warehouse equipment (OSHA Class II, narrow-aisle reach), NOT counterbalance.
All models are stand-up, electric, moving-mast reach trucks with poly tires and a
24 in (610 mm) load center.

Series convention (per the user): store the SUB-SERIES only, not the marketing
model-range string. So "ZR3", "ZDR3", "ZRS3", "ZDRS3" — never "N35-45ZR3".

Model-code decoding, confirmed against the Hyster technical guides:
  N  = narrow aisle
  Z  = moving-mast reach
  D  = double / deep reach (two-pallet-deep racking); absent = single reach
  R  = reach
  S  = side stance operator compartment; absent = fore/aft stance
  3  = third generation (Australian/APAC catalogs drop the generation digit,
       e.g. "N35ZR" — treated as the same truck here)

Spec provenance (all figures below are from these three sources):
  * Hyster NA product page   — capacities, max fork heights, voltages
  * N35-45ZR3 / N30-35ZDR3 Technical Guide (fore/aft stance, 36V)
  * N35-40ZRS3 / N30ZDRS3 Technical Guide (side stance, 24V/36V)
Aisle figures are ZERO-CLEARANCE right-angle-stack ranges across 36-48 in
pallets; Hyster notes an extra 6-12 in should be added for real maneuvering.

Production years are ESTIMATED: Hyster does not publish a launch date for the
"3" generation. The technical guides carry Hyster part numbers beginning 2015
and both are (c) 2024 with the series still current, so year_start=2015 and
year_end=None (still in production). Flagged as an estimate in notes.

Dedupes by (manufacturer='Hyster', model) — safe to re-run. When an existing row
is found, BACKFILL_EXISTING controls whether its series/specs are normalized to
the values below (the DB already carries one N45ZR3 with series "N35-45ZR3").

Usage:  python hyster_reach_add.py
"""
import csv
from typing import NamedTuple

from app.database import SessionLocal
from app.main import next_company_serial
from app.models import Forklift

HYSTER = "Hyster"
REPORT = "hyster_reach_report.csv"
TRUCK_CLASS = "Class II"          # OSHA Class II = narrow-aisle / reach
FUEL = "Electric"                 # reach trucks are always electric

SRC = ("https://www.hyster.com/en-us/north-america/"
       "industrial-warehouse-and-manufacturing-lift-trucks/n-zr3-zdr3-n-zrs3-zdrs3/")
TG_FORE_AFT = ("https://www.hyster.com/globalassets/coms/hyster/north-america/documents/"
               "trucks/reach-trucks/2015hbc2sp001-e-en-us-reach-truck-warehouse-tech-guide.pdf")
TG_SIDE = ("https://www.hyster.com/globalassets/coms/hyster/north-america/documents/"
           "trucks/reach-trucks/2015hbc2sp002-e-en-us-reach-truck-retail-tech-guide.pdf")

YEAR_START = 2015                 # estimated — see module docstring
YEAR_END = None                   # still in production

# Set False to leave pre-existing rows completely untouched.
BACKFILL_EXISTING = True


class Reach(NamedTuple):
    model: str            # full model code, e.g. N45ZR3
    series: str           # sub-series only: ZR3 / ZDR3 / ZRS3 / ZDRS3
    capacity_kg: float
    capacity_lb: int
    reach_type: str       # Single reach | Double reach (deep reach)
    stance: str           # Fore/aft stance | Side stance
    max_fork_height_in: int
    aisle_in: str         # zero-clearance right-angle stack range
    turning_radius_in: str
    voltage: str
    weight_lb: int        # truck weight without battery
    pdf_url: str

    @property
    def notes(self) -> str:
        return (
            f"{self.reach_type}, {self.stance}. "
            f"Capacity {self.capacity_lb} lb ({self.capacity_kg:.0f} kg) @ 24 in load center. "
            f"Max fork height {self.max_fork_height_in} in. "
            f"Zero-clearance right-angle-stack aisle {self.aisle_in} in "
            f"(36-48 in pallets; add 6-12 in for maneuvering). "
            f"Min outside turning radius {self.turning_radius_in} in. "
            f"{self.voltage}, stand-up operator, poly tires. "
            f"Class II narrow-aisle warehouse reach truck (not counterbalance). "
            f"APAC/Australian catalogs drop the generation digit "
            f"(listed as {self.model.replace('3', '', 1) if self.model.endswith('3') else self.model}). "
            f"Production years estimated ({YEAR_START}-present); Hyster publishes no launch date."
        )


MODELS = [
    # ---- ZR3: single reach, fore/aft stance, 36V ----
    Reach("N35ZR3", "ZR3", 1588, 3500, "Single reach", "Fore/aft stance",
          302, "85-96", "65.6-67.6", "36V", 5400, TG_FORE_AFT),
    Reach("N40ZR3", "ZR3", 1814, 4000, "Single reach", "Fore/aft stance",
          368, "85-101", "65.6-68.5", "36V", 5400, TG_FORE_AFT),
    Reach("N45ZR3", "ZR3", 2041, 4500, "Single reach", "Fore/aft stance",
          444, "88-101", "68.5-73.5", "36V", 5930, TG_FORE_AFT),

    # ---- ZDR3: double/deep reach, fore/aft stance, 36V ----
    Reach("N30ZDR3", "ZDR3", 1361, 3000, "Double reach (deep reach)", "Fore/aft stance",
          368, "86-105", "70.6-72.6", "36V", 5650, TG_FORE_AFT),
    Reach("N35ZDR3", "ZDR3", 1588, 3500, "Double reach (deep reach)", "Fore/aft stance",
          444, "90-105", "72.6-77.6", "36V", 6190, TG_FORE_AFT),

    # ---- ZRS3: single reach, side stance, 24V/36V ----
    Reach("N35ZRS3", "ZRS3", 1588, 3500, "Single reach", "Side stance",
          272, "85-96", "65.7-67.6", "24V/36V", 5340, TG_SIDE),
    Reach("N40ZRS3", "ZRS3", 1814, 4000, "Single reach", "Side stance",
          272, "85-96", "65.7-67.6", "24V/36V", 5360, TG_SIDE),

    # ---- ZDRS3: double reach, side stance, 24V/36V ----
    Reach("N30ZDRS3", "ZDRS3", 1361, 3000, "Double reach (deep reach)", "Side stance",
          272, "86-100", "70.5-72.4", "24V/36V", 5590, TG_SIDE),
]

COLUMNS = ["status", "manufacturer", "series", "model", "capacity_kg", "capacity_lb",
           "fuel_type", "truck_class", "reach_type", "stance", "max_fork_height_in",
           "aisle_width_in", "turning_radius_in", "voltage", "weight_lb",
           "year_start", "year_end", "internal_serial", "source_url", "pdf_url", "notes"]


def _row(status: str, b: Reach, serial: str | None) -> list:
    return [status, HYSTER, b.series, b.model, b.capacity_kg, b.capacity_lb,
            FUEL, TRUCK_CLASS, b.reach_type, b.stance, b.max_fork_height_in,
            b.aisle_in, b.turning_radius_in, b.voltage, b.weight_lb,
            YEAR_START, YEAR_END or "", serial or "", SRC, b.pdf_url, b.notes]


def main() -> None:
    db = SessionLocal()
    existing = {f.model: f
                for f in db.query(Forklift).filter_by(manufacturer=HYSTER).all()}
    added = updated = skipped = 0

    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)

        for b in MODELS:
            found = existing.get(b.model)

            if found is not None:
                if not BACKFILL_EXISTING:
                    skipped += 1
                    w.writerow(_row("EXISTS", b, found.internal_serial))
                    continue
                # Normalize the pre-existing row onto the researched values.
                found.series = b.series
                found.capacity_kg = b.capacity_kg
                found.fuel_type = FUEL
                found.truck_class = TRUCK_CLASS
                found.year_start = YEAR_START
                found.year_end = YEAR_END
                found.source_url = SRC
                found.pdf_url = b.pdf_url
                found.notes = b.notes
                if not found.internal_serial:
                    db.flush()
                    found.internal_serial = next_company_serial(db)
                db.flush()
                updated += 1
                w.writerow(_row("UPDATED", b, found.internal_serial))
                continue

            # Corrected serial assignment pattern: add, flush, assign serial, flush.
            nf = Forklift(
                manufacturer=HYSTER,
                model=b.model,
                series=b.series,
                capacity_kg=b.capacity_kg,
                fuel_type=FUEL,
                truck_class=TRUCK_CLASS,
                year_start=YEAR_START,
                year_end=YEAR_END,
                source_url=SRC,
                pdf_url=b.pdf_url,
                notes=b.notes,
            )
            db.add(nf)
            db.flush()
            nf.internal_serial = next_company_serial(db)
            db.flush()

            existing[b.model] = nf
            added += 1
            w.writerow(_row("ADDED", b, nf.internal_serial))

        db.commit()

    total = db.query(Forklift).filter_by(manufacturer=HYSTER).count()
    reach_total = (db.query(Forklift)
                   .filter(Forklift.manufacturer == HYSTER,
                           Forklift.series.in_(["ZR3", "ZDR3", "ZRS3", "ZDRS3"]))
                   .count())
    db.close()

    print(f"Added {added} new Hyster reach trucks "
          f"({updated} existing rows normalized, {skipped} skipped).")
    print(f"Hyster total now {total}; ZR3/ZDR3/ZRS3/ZDRS3 rows: {reach_total}.")
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
