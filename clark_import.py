"""Import Clark's counterbalance forklift lineup from clarkmhc.com/all-forklifts/.

Series rules (from the user): any model beginning with 'S' is the S Series; SE
models are Clark's electric-pneumatic line. Other families keep their own series
(C, CGC, GEX, GTS). Capacity ranges/type from the site go into notes for review.

Ranges on the site are expanded to individual models (best-effort — verify the
exact members against the model pages/brochures). Warehouse gear (reach, tow,
order pickers, pallet jacks/stackers) is NOT included here — add on request.

Safe to re-run: dedupes by (manufacturer='Clark', model).
Usage:  python clark_import.py
"""
import csv

from app.database import SessionLocal
from app.main import next_company_serial
from app.models import Forklift

CLARK = "Clark"
SRC = "https://www.clarkmhc.com/all-forklifts/"
REPORT = "clark_import_report.csv"

# (model, series, fuel_type, notes)
MODELS = [
    # ---- S Series (any S-prefix) ----
    ("S20",  "S Series", "LPG", "IC pneumatic, ~4,000 lb"),
    ("S25",  "S Series", "LPG", "IC pneumatic, ~5,000 lb"),
    ("S30",  "S Series", "LPG", "IC pneumatic, ~6,000 lb"),
    ("S35",  "S Series", "LPG", "IC pneumatic, ~7,000 lb"),
    ("S20C", "S Series", "LPG", "IC cushion / dual-fuel"),
    ("S25C", "S Series", "LPG", "IC cushion / dual-fuel"),
    ("S30C", "S Series", "LPG", "IC cushion / dual-fuel"),
    ("S32C", "S Series", "LPG", "IC cushion / dual-fuel, ~6,500 lb"),
    ("S40",  "S Series", "LPG", "IC pneumatic LPG/Diesel, 8,000-12,000 lb"),
    ("S45",  "S Series", "LPG", "IC pneumatic LPG/Diesel, 8,000-12,000 lb"),
    ("S50",  "S Series", "LPG", "IC pneumatic LPG/Diesel, 8,000-12,000 lb"),
    ("S55",  "S Series", "LPG", "IC pneumatic LPG/Diesel, 8,000-12,000 lb"),
    ("S60",  "S Series", "LPG", "IC pneumatic LPG/Diesel, 8,000-12,000 lb"),
    ("SE15T", "S Series", "Electric", "Electric sit-down 36V/48V, ~3,000 lb"),
    ("SE20T", "S Series", "Electric", "Electric sit-down 36V/48V, ~4,000 lb"),
    ("SE25T", "S Series", "Electric", "Electric sit-down 36V/48V, ~5,000 lb"),
    ("SEC20", "S Series", "Electric", "Electric cushion 36V/48V"),
    ("SEC25", "S Series", "Electric", "Electric cushion 36V/48V"),
    ("SEC30", "S Series", "Electric", "Electric cushion 36V/48V"),
    ("SEC35", "S Series", "Electric", "Electric cushion 36V/48V, ~7,000 lb"),
    ("SES15", "S Series", "Electric", "Electric sit-down 36V/48V"),
    ("SES20", "S Series", "Electric", "Electric sit-down 36V/48V"),
    ("SES25", "S Series", "Electric", "Electric sit-down 36V/48V"),
    ("SE25",  "S Series", "Electric", "Electric PNEUMATIC 80V, ~5,000 lb"),
    ("SE30",  "S Series", "Electric", "Electric PNEUMATIC 80V, ~6,000 lb"),
    ("SE35",  "S Series", "Electric", "Electric PNEUMATIC 80V, ~7,000 lb"),
    ("SE16",  "S Series", "Electric", "Electric (STE/SE 16-20), ~3,200 lb"),
    ("SE20",  "S Series", "Electric", "Electric (STE/SE 16-20), ~4,000 lb"),
    ("STE16", "S Series", "Electric", "Electric (STE/SE 16-20)"),
    ("STE20", "S Series", "Electric", "Electric (STE/SE 16-20)"),
    ("S25XE", "S Series", "Electric", "Renegade, Lithium, 5,000-7,000 lb"),
    # ---- C Series ----
    ("C15",  "C Series", "LPG", "IC pneumatic, 3,000-4,000 lb"),
    ("C15C", "C Series", "LPG", "IC cushion, 3,000-4,000 lb"),
    ("C60",  "C Series", "LPG", "IC pneumatic LPG/Diesel, 13,500-18,000 lb"),
    # ---- CGC (cushion) ----
    ("CGC40", "CGC Series", "LPG", "IC cushion, 8,000-12,000 lb"),
    ("CGC70", "CGC Series", "LPG", "IC cushion, 13,500-15,500 lb"),
    # ---- GEX (electric pneumatic) ----
    ("GEX40", "GEX Series", "Electric", "Electric pneumatic 80V, 8,000 lb"),
    ("GEX45", "GEX Series", "Electric", "Electric pneumatic 80V"),
    ("GEX50", "GEX Series", "Electric", "Electric pneumatic 80V, 10,000 lb"),
    # ---- GTS ----
    ("GTS25", "GTS Series", "LPG", "IC pneumatic, 4,000-6,600 lb"),
]


def main() -> None:
    db = SessionLocal()
    existing = {f.model for f in db.query(Forklift).filter_by(manufacturer=CLARK).all()}
    added = 0
    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["model", "series", "fuel_type", "notes"])
        for model, series, fuel, notes in MODELS:
            if model in existing:
                w.writerow([model, series, fuel, "EXISTS"])
                continue
            db.add(Forklift(manufacturer=CLARK, model=model, series=series,
                            fuel_type=fuel, source_url=SRC, notes=notes,
                            internal_serial=next_company_serial(db)))
            db.flush()
            existing.add(model)
            added += 1
            w.writerow([model, series, fuel, notes])
        db.commit()
    total = db.query(Forklift).filter_by(manufacturer=CLARK).count()
    db.close()
    print(f"Added {added} Clark models. Clark total now {total}. Report: {REPORT}")


if __name__ == "__main__":
    main()
