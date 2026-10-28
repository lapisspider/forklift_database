"""Order-record intake and range derivation -- app/serial_intake.py + app/fitment_derive.py.

Run on its own:       .venv\\Scripts\\python.exe tests\\test_serial_intake.py
or as section 18 of:  .venv\\Scripts\\python.exe tests\\stress_lookup.py

Free and offline. Unlike section 17 this builds its OWN database in a temp file
and never touches `data/forklifts.db`: the cases need customer names, locations
and order references, and inventing those in the real database would leave
fabricated orders sitting next to real ones with nothing to tell them apart.

What the cases defend, worst mistake first:

  1. A documented bound is never edited. The sheet's range and the field's range
     have to stay tellable apart, or a rule that has drifted somewhere wrong
     looks exactly like a rule that was always right.
  2. Derivation never narrows and never ranges an unordered scheme. The absence
     of an order record is not evidence of anything, and Doosan serials do not
     count upwards at all.
  3. Re-importing adds nothing. Order management will send overlapping files.
  4. Nothing is thrown away for being unrecognised -- an unknown part number and
     an unreadable serial are both still recorded.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import fitment as F  # noqa: E402
from app import fitment_derive as D  # noqa: E402
from app import serial_intake as I  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Kit, KitFitment, SerialRecord  # noqa: E402

MCA1 = "174-MCA1-00G05-01"
MCA2 = "174-MCA2-00G05-01"

# Headers deliberately spelled the way an export would, not the way the fields
# are named: the mapping has to work on meaning.
CSV = """Order No,Part Number,Truck Make,Model Number,Serial Number,Customer Name,Site
SO-1001,174-MCA1-00G05-01,CAT/Mitsubishi,GP40K,05000,Acme Freight,"Dallas, TX"
SO-1002,174-MCA1-00G05-01,CAT/Mitsubishi,GP45K,00800,Borden Cold Store,"Omaha, NE"
SO-1003,174-MCA2-00G05-01,CAT/Mitsubishi,GP40K,11500,Cutler Logistics,"Reno, NV"
SO-1004,174-BB15T7P-00G05,Bobcat/Doosan,B15T-7P,L7-00116,Dalton Paper,"Mobile, AL"
SO-1005,174-NOT-A-REAL-KIT,CAT/Mitsubishi,GP40K,06000,Evans Produce,"Fresno, CA"
SO-1001,174-MCA1-00G05-01,CAT/Mitsubishi,GP40K,05000,Acme Freight,"Dallas, TX"
SO-1006,174-MCA1-00G05-01,CAT/Mitsubishi,GP40K,,Fisher Hauling,"Boise, ID"
"""

# The record that puts a kit past its documented bound: MCA1's sheet stops at
# 10230 and order management supplied one at 10400.
CSV_BEYOND = """Order No,Part Number,Truck Make,Model Number,Serial Number,Customer Name,Site
SO-2001,174-MCA1-00G05-01,CAT/Mitsubishi,GP40K,10400,Gray Transfer,"Tulsa, OK"
"""


def _fresh_db(tmp: Path):
    """A database holding just the MCA pair and their documented rules."""
    engine = create_engine(f"sqlite:///{tmp.as_posix()}")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    k1 = Kit(sku=MCA1, name="MCA1")
    k2 = Kit(sku=MCA2, name="MCA2")
    k3 = Kit(sku="174-BB15T7P-00G05", name="Doosan B15T")
    db.add_all([k1, k2, k3])
    db.flush()
    db.add_all([
        KitFitment(kit_id=k1.id, manufacturer="CAT/Mitsubishi",
                   model_expr="GP40-50K", condition="range",
                   serial_lo=1, serial_hi=10230, origin="documented",
                   source_note="kit sheet: (serial # 00001-10230)"),
        KitFitment(kit_id=k2.id, manufacturer="CAT/Mitsubishi",
                   model_expr="GP40-50K", condition="range",
                   serial_lo=10231, serial_hi=None, origin="documented",
                   source_note="kit sheet: (serial # 10231+)"),
        KitFitment(kit_id=k3.id, manufacturer="Bobcat/Doosan",
                   model_expr="B15T-7P", condition="always",
                   origin="documented"),
    ])
    db.commit()
    return db, engine


def run(R) -> None:
    R.start("18. Order-record intake and range derivation")

    with tempfile.TemporaryDirectory() as tmpdir:
        db, engine = _fresh_db(Path(tmpdir) / "intake.db")
        try:
            _cases(R, db, Path(tmpdir))
        finally:
            db.close()
            engine.dispose()


def _cases(R, db, tmpdir: Path) -> None:
    # --- headers by meaning, not position --------------------------------
    report = I.read_csv(db, CSV)
    R.check("every required column was matched", not report.missing_fields,
            f"missing {report.missing_fields}")
    R.check("'Order No' mapped to order_ref",
            report.mapping.get("Order No") == "order_ref", f"{report.mapping}")
    R.check("'Truck Make' mapped to manufacturer",
            report.mapping.get("Truck Make") == "manufacturer")
    R.check("'Site' mapped to location",
            report.mapping.get("Site") == "location")

    bad = I.read_csv(db, "Part Number,Serial Number\n174-MCA1-00G05-01,05000\n")
    R.check("a CSV with no model or OEM column is refused",
            set(bad.missing_fields) == {"manufacturer", "model"},
            f"missing {bad.missing_fields}")

    # --- rows judged individually ----------------------------------------
    R.check("the blank-serial row is rejected", len(report.failed) == 1,
            f"{[(r.line, r.errors) for r in report.failed]}")
    R.check("and says which field was empty",
            report.failed and "serial is empty" in report.failed[0].errors)
    R.check("the line repeated inside the file is skipped",
            len(report.duplicates) == 1,
            f"{[(r.line, r.duplicate_of) for r in report.duplicates]}")
    R.check("5 rows are left to write", len(report.writable) == 5,
            f"{len(report.writable)}")

    unknown = [r for r in report.rows
               if r.data.get("part_number") == "174-NOT-A-REAL-KIT"]
    R.check("an unknown part number is a warning, not an error",
            unknown and unknown[0].ok and unknown[0].warnings,
            f"{unknown[0].errors if unknown else 'row missing'}")
    R.check("and it is still written with no kit link",
            unknown and unknown[0].writable and unknown[0].kit_id is None)

    doosan = [r for r in report.rows if r.data.get("serial") == "L7-00116"]
    R.check("an unplaceable serial is a warning, not an error",
            doosan and doosan[0].writable and doosan[0].warnings,
            f"{doosan[0].errors if doosan else 'row missing'}")
    R.check("and the warning says why in plain words",
            doosan and "batch" in doosan[0].warnings[0].lower(),
            f"{doosan[0].warnings if doosan else None}")

    # --- writing, then re-importing --------------------------------------
    written = I.commit(db, report)
    R.check("5 records written", len(written) == 5, f"{len(written)}")
    R.check("records total 5", db.query(SerialRecord).count() == 5)

    placed = db.query(SerialRecord).filter(SerialRecord.serial == "05000").first()
    R.check("a placeable serial stored its sequence",
            placed is not None and placed.serial_seq == 5000,
            f"{placed.serial_seq if placed else None}")
    unplaced = db.query(SerialRecord).filter(
        SerialRecord.serial == "L7-00116").first()
    R.check("an unplaceable serial stored no sequence",
            unplaced is not None and unplaced.serial_seq is None)
    R.check("but kept the raw serial and a normalised form",
            unplaced is not None and unplaced.serial == "L7-00116"
            and unplaced.serial_norm == "L700116",
            f"{unplaced.serial_norm if unplaced else None}")

    again = I.read_csv(db, CSV)
    R.check("re-importing the same CSV writes nothing",
            len(again.writable) == 0,
            f"{[(r.line, r.data.get('serial')) for r in again.writable]}")
    I.commit(db, again)
    R.check("and the record count is unchanged",
            db.query(SerialRecord).count() == 5)

    # --- derivation ------------------------------------------------------
    derived = D.apply(db)
    R.check("observed ranges were derived", derived.written > 0,
            f"wrote {derived.written}")
    R.check("no Doosan record produced a range",
            not any(e.manufacturer == "Bobcat/Doosan" for e in derived.envelopes),
            "ranging a batch-allocated serial is meaningless")
    R.check("the Doosan record is reported as unplaceable instead",
            any(r.serial == "L7-00116" for r in derived.unplaceable))
    R.check("the unknown part number is reported as unlinked",
            any(r.part_number == "174-NOT-A-REAL-KIT" for r in derived.unlinked))

    docs = db.query(KitFitment).filter(KitFitment.origin == "documented").all()
    mca1_doc = [d for d in docs if d.kit.sku == MCA1][0]
    R.check("the documented bound was not edited",
            (mca1_doc.serial_lo, mca1_doc.serial_hi) == (1, 10230),
            f"became {mca1_doc.serial_lo}-{mca1_doc.serial_hi}")

    obs = db.query(KitFitment).filter(KitFitment.origin == "observed").all()
    mca1_obs = [o for o in obs if o.kit.sku == MCA1]
    R.check("an observed row sits beside it", len(mca1_obs) == 1, f"{len(mca1_obs)}")
    R.check("the observed row spans only what was actually supplied",
            mca1_obs and (mca1_obs[0].serial_lo, mca1_obs[0].serial_hi) == (800, 5000),
            f"{(mca1_obs[0].serial_lo, mca1_obs[0].serial_hi) if mca1_obs else None}")
    R.check("and says it came from the field, not the sheet",
            mca1_obs and "order record" in (mca1_obs[0].source_note or ""),
            f"{mca1_obs[0].source_note if mca1_obs else None}")

    # Observed rows are derived data: a second pass replaces them, never stacks.
    before = db.query(KitFitment).filter(KitFitment.origin == "observed").count()
    second = D.apply(db)
    after = db.query(KitFitment).filter(KitFitment.origin == "observed").count()
    R.check("a second pass replaces the observed rows rather than stacking them",
            before == after, f"{before} -> {after}")
    R.check("and it says how many it replaced", second.removed == before,
            f"removed {second.removed} of {before}")

    # --- a record past the documented bound ------------------------------
    beyond = I.read_csv(db, CSV_BEYOND)
    R.check("the out-of-range order record imports cleanly",
            len(beyond.writable) == 1 and not beyond.failed,
            f"{[(r.line, r.errors) for r in beyond.rows]}")
    I.commit(db, beyond)
    derived = D.apply(db)

    R.check("it is flagged as reaching past the documented bound",
            any(e.model_expr == "GP40-50K" and d.kit.sku == MCA1
                for e, d in derived.beyond_documented),
            f"{[(e.model_expr, d.kit.sku) for e, d in derived.beyond_documented]}")
    mca1_doc = db.query(KitFitment).filter(
        KitFitment.origin == "documented",
        KitFitment.kit_id == mca1_doc.kit_id).first()
    R.check("the documented bound STILL was not edited",
            (mca1_doc.serial_lo, mca1_doc.serial_hi) == (1, 10230),
            f"became {mca1_doc.serial_lo}-{mca1_doc.serial_hi}")
    mca1_obs = [o for o in db.query(KitFitment).filter(
        KitFitment.origin == "observed").all() if o.kit.sku == MCA1]
    R.check("the observed row widened to reach it",
            mca1_obs and mca1_obs[0].serial_hi == 10400,
            f"{mca1_obs[0].serial_hi if mca1_obs else None}")
    R.check("the needs-attention line names both bounds",
            any("10230" in line and "10400" in line
                for line in D.describe(db, derived)),
            f"{[l for l in D.describe(db, derived) if 'ATTENTION' in l or '10400' in l]}")

    # One truck, two kits: 10400 is inside MCA1's observed range and inside
    # MCA2's documented 10231+. Both claim it, and that is for a person.
    hits = F.kits_for(db, "CAT/Mitsubishi", "GP40K", "10400")
    R.check("both kits come back for the overlapping serial",
            {MCA1, MCA2} <= {h.sku for h in F.confirmed(hits)},
            f"{[(h.sku, h.verdict) for h in hits]}")

    # --- an order record outranks a rule that excludes it ----------------
    # Serial 10400 is outside MCA1's DOCUMENTED range, but a truck at 10400 was
    # supplied MCA1. The record wins, and says so.
    mca1_hit = [h for h in hits if h.sku == MCA1][0]
    R.check("the supplied kit is reported as supplied, not merely confirmed",
            mca1_hit.verdict == F.SUPPLIED, f"{mca1_hit.verdict}")
    R.check("and the reason names the customer",
            "Gray Transfer" in mca1_hit.reason, f"{mca1_hit.reason}")
    R.check("and the record itself is attached", len(mca1_hit.records) == 1)
    R.check("supplied sorts above confirmed", hits[0].verdict == F.SUPPLIED,
            f"{[(h.sku, h.verdict) for h in hits]}")

    # The Doosan truck: no range can ever confirm it, but the exact serial can.
    hits = F.kits_for(db, "Bobcat/Doosan", "B15T-7P", "l7 00116")
    doo = [h for h in hits if h.sku == "174-BB15T7P-00G05"]
    R.check("an unordered-scheme truck is still matched by its exact serial",
            doo and doo[0].verdict == F.SUPPLIED,
            f"{[(h.sku, h.verdict) for h in hits]}")
    R.check("and the serial matched despite the spacing",
            doo and len(doo[0].records) == 1)

    # --- superseding a record takes it out of the range ------------------
    rec = db.query(SerialRecord).filter(SerialRecord.serial == "10400").first()
    rec.status = "superseded"
    db.commit()
    derived = D.apply(db)
    mca1_obs = [o for o in db.query(KitFitment).filter(
        KitFitment.origin == "observed").all() if o.kit.sku == MCA1]
    R.check("superseding the record shrinks the observed range back",
            mca1_obs and mca1_obs[0].serial_hi == 5000,
            f"{mca1_obs[0].serial_hi if mca1_obs else None}")
    R.check("and the needs-attention flag clears with it",
            not derived.beyond_documented,
            f"{[(e.model_expr, d.kit.sku) for e, d in derived.beyond_documented]}")
    R.check("the superseded record is kept, not deleted",
            db.query(SerialRecord).filter(
                SerialRecord.status == "superseded").count() == 1)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from stress_lookup import R  # noqa: E402

    run(R)
    raise SystemExit(R.summary())
