"""Import BYD's forklift lineup as individual models, with chassis groupings.

Curated from BYD's US site (en.byd.com/forklift) + web verification. Each capacity
variant is its own row. series = product family (ECB/ECC/PMW/T); chassis = the
shared-chassis frame group. Capacity for ECB/ECC = model number x 100 kg. All BYD
forklifts are electric (LFP battery).

Current North-American models are high confidence. Older/global lines (notably the
"CPD" electric-counterbalance naming) appear to be regional aliases of the same ECB
trucks, so they are NOT inserted as duplicates — they're flagged in the report for
your review instead.

Safe to re-run: dedupes by (manufacturer='BYD', model).
Usage:  python byd_import.py
"""
import csv

from app.database import SessionLocal
from app.main import next_company_serial
from app.models import Forklift

BYD = "BYD"
BASE = "https://en.byd.com/forklift/"
REPORT = "byd_import_report.csv"

# (model, series, capacity_kg, chassis, status, source_slug)
MODELS = [
    # ECB — pneumatic sit-down counterbalance (3-wheel and 4-wheel "S" are distinct)
    ("ECB 16",  "ECB", 1600, "Compact 3-Wheel Frame",  "current", "ecb16"),
    ("ECB 18",  "ECB", 1800, "Compact 3-Wheel Frame",  "current", "ecb18"),
    ("ECB 16S", "ECB", 1600, "Compact 4-Wheel Frame",  "current", "ecb16s"),
    ("ECB 18S", "ECB", 1800, "Compact 4-Wheel Frame",  "current", "ecb18s"),
    ("ECB 20",  "ECB", 2000, "Mid-Size Frame",         "current", "ecb20"),
    ("ECB 25",  "ECB", 2500, "Mid-Size Frame",         "current", "ecb25"),
    ("ECB 27",  "ECB", 2700, "Mid-Size Frame",         "current", "ecb27"),
    ("ECB 30",  "ECB", 3000, "Heavy-Duty Mid Frame",   "current", "ecb30"),
    ("ECB 35",  "ECB", 3500, "Heavy-Duty Mid Frame",   "current", "ecb35"),
    ("ECB 40",  "ECB", 4000, "Large-Capacity Frame",   "current", "ecb40"),
    ("ECB 45",  "ECB", 4500, "Large-Capacity Frame",   "current", "ecb45"),
    ("ECB 50",  "ECB", 5000, "Large-Capacity Frame",   "current", "ecb50"),
    # ECC — cushion-tire counterbalance (chassis grouping not yet verified)
    ("ECC 22",  "ECC", 2200, None, "current", "ecc22"),
    ("ECC 27",  "ECC", 2700, None, "current", "ecc27"),
    ("ECC 32",  "ECC", 3200, None, "current", "ecc32"),
    # Warehouse gear
    ("PMW 20",  "PMW", 2000, None, "current", "pmw20"),   # electric pallet truck
    ("T 50",    "T",   None, None, "current", "t50"),     # tow tractor (tow capacity, not lift)
]

# Items to flag for the user rather than insert (unverified / likely duplicates).
FLAGS = [
    ("CPD 15/18/20/25/30/35/45/50", "CPD", "",
     "review", "BYD's global 'CPD' counterbalance naming appears to be a regional "
     "alias of the ECB line (same trucks). Not inserted to avoid duplicates — "
     "confirm if any are genuinely distinct older models to add."),
    ("Reach trucks / stackers", "?", "",
     "review", "BYD makes reach trucks & stackers in some markets; none are listed "
     "on the US site, so specific model numbers are unverified. Add if SSC needs them."),
    ("ECC chassis groups", "ECC", "",
     "review", "ECC 22/27/32 chassis groupings were not web-verified; left blank."),
]


def main() -> None:
    db = SessionLocal()

    # 1. Fix the existing 'ECB18' row -> 'ECB 18', series ECB, chassis set. Keep kit link.
    old = db.query(Forklift).filter(Forklift.manufacturer == BYD,
                                    Forklift.model == "ECB18").first()
    if old:
        old.model = "ECB 18"
        old.series = "ECB"
        old.chassis = "Compact 3-Wheel Frame"
        if not old.capacity_kg:
            old.capacity_kg = 1800
        old.fuel_type = old.fuel_type or "Electric"
        db.commit()
        print("Updated existing ECB18 -> 'ECB 18' (kept kit link)")

    existing = {f.model.lower() for f in db.query(Forklift).filter_by(manufacturer=BYD).all()}
    added = 0
    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "series", "capacity_kg", "chassis", "status", "source"])
        for model, series, cap, chassis, status, slug in MODELS:
            src = BASE + slug + "/"
            if model.lower() in existing:
                w.writerow([model, series, cap, chassis, "exists", src])
                continue
            db.add(Forklift(
                manufacturer=BYD, model=model, series=series,
                capacity_kg=cap, fuel_type="Electric", chassis=chassis,
                source_url=src, notes=("Tow tractor — value is tow capacity" if series == "T" else None),
                internal_serial=next_company_serial(db),
            ))
            db.flush()
            existing.add(model.lower())
            added += 1
            w.writerow([model, series, cap, chassis, status, src])
        db.commit()
        for row in FLAGS:
            w.writerow([row[0], row[1], "", row[2], row[3], row[4]])

    total = db.query(Forklift).filter_by(manufacturer=BYD).count()
    db.close()
    print(f"Added {added} BYD models. BYD total now {total}. "
          f"Review flags + rows in {REPORT}.")


if __name__ == "__main__":
    main()
