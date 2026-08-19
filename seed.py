"""Seed the database with a few sample forklifts so the UI isn't empty.

Run once:  python seed.py
Safe to re-run — it skips models that already exist.
"""
from app.database import SessionLocal, init_db
from app.models import Forklift

SAMPLES = [
    dict(manufacturer="Toyota", series="8-Series", model="8FGCU25",
         capacity_kg=2500, fuel_type="LPG", notes="Sample record."),
    dict(manufacturer="Hyster", series="Fortis", model="H2.5FT",
         capacity_kg=2500, fuel_type="diesel", notes="Sample record."),
    dict(manufacturer="Crown", series="FC 5200 Series", model="FC 5252",
         capacity_kg=2270, fuel_type="electric", notes="Sample record."),
]


def main() -> None:
    init_db()
    db = SessionLocal()
    added = 0
    for s in SAMPLES:
        exists = db.query(Forklift).filter_by(
            manufacturer=s["manufacturer"], model=s["model"]).first()
        if not exists:
            db.add(Forklift(**s))
            added += 1
    db.commit()
    # Assign company serials to any forklift that lacks one (FLT-<id>).
    for fk in db.query(Forklift).filter(Forklift.internal_serial.is_(None)).all():
        fk.internal_serial = f"FLT-{fk.id:04d}"
    db.commit()
    db.close()
    print(f"Seed complete. Added {added} new record(s).")


if __name__ == "__main__":
    main()
