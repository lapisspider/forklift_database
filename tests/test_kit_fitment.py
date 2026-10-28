"""Kit fitment keyed on serials -- app/fitment.py over the migrated rows.

Run on its own:       .venv\\Scripts\\python.exe tests\\test_kit_fitment.py
or as section 17 of:  .venv\\Scripts\\python.exe tests\\stress_lookup.py

Free and offline, but it DOES read the database: these cases check the rows
`migrate_kit_fitment.py` wrote, not a fixture, because the thing most likely to
be wrong is the reading of a kit sheet rather than the comparison code.

The case that matters most is the MCA1/MCA2 pair. Both kits fit a GP40-50K and
only the serial separates them, so the same query with two serials has to return
two different part numbers. If it ever returns both, or the wrong one, a
salesperson quotes a part that does not fit and nothing in the output says so.

The rest of the cases are about the refusals, for the same reason as
test_serials.py: a verdict of `unconfirmed` must never be reported as a fit, and
`excluded` must never be reported as a refusal to answer.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import fitment as F  # noqa: E402
from app.models import KitFitment  # noqa: E402

MCA1 = "174-MCA1-00G05-01"
MCA2 = "174-MCA2-00G05-01"


def run(R, db) -> None:
    R.start("17. Kit fitment -- the serial decides which part number")

    total = db.query(KitFitment).count()
    R.check("kit_fitment is populated", total > 0,
            "run migrate_kit_fitment.py --apply")
    if not total:
        return

    # --- the pair this schema exists for ---------------------------------
    # `GP40K` is the single truck a salesperson types; it sits inside both
    # kits' `GP40-50K`, so the serial is the only thing that separates them.
    for serial, want in (("00001", MCA1), ("05000", MCA1), ("10230", MCA1),
                         ("10231", MCA2), ("12000", MCA2)):
        hits = F.kits_for(db, "CAT/Mitsubishi", "GP40K", serial)
        got = sorted({h.sku for h in F.confirmed(hits)})
        R.check(f"GP40K serial {serial} -> {want}", got == [want],
                f"confirmed {got}; all verdicts "
                f"{[(h.sku, h.verdict) for h in hits]}")

    # The sheet's own wording is also what gets pasted in, and must resolve the
    # same way.
    got = sorted({h.sku for h in F.confirmed(
        F.kits_for(db, "CAT/Mitsubishi", "GP40-50K", "05000"))})
    R.check("the range expression itself resolves too", got == [MCA1], f"got {got}")

    # The losing kit must come back as a stated exclusion, not as silence --
    # it is the reason the other one is right.
    hits = F.kits_for(db, "CAT/Mitsubishi", "GP40K", "12000")
    excluded = {h.sku for h in hits if h.verdict == F.EXCLUDED}
    R.check("the out-of-range kit is shown as excluded, not dropped",
            MCA1 in excluded, f"excluded {excluded}")
    R.check("an excluded hit says which range it fell outside",
            any("outside" in h.reason for h in hits if h.verdict == F.EXCLUDED))

    # --- no serial: everything serial-dependent is unconfirmed ------------
    hits = F.kits_for(db, "CAT/Mitsubishi", "GP40K", None)
    R.check("without a serial neither MCA kit is confirmed",
            not {h.sku for h in F.confirmed(hits)} & {MCA1, MCA2},
            f"confirmed {[h.sku for h in F.confirmed(hits)]}")
    R.check("without a serial both MCA kits are offered as unconfirmed",
            {MCA1, MCA2} <= {h.sku for h in hits if h.verdict == F.UNCONFIRMED})

    # --- a model no rule names is not the same as 'no kit fits' -----------
    R.check("an unknown model returns nothing at all",
            F.kits_for(db, "CAT/Mitsubishi", "ZZ999-NOPE", "00001") == [])

    # --- an unordered OEM must refuse, in plain words --------------------
    doosan = (db.query(KitFitment)
              .filter(KitFitment.manufacturer == "Bobcat/Doosan").first())
    if doosan is not None:
        hits = F.kits_for(db, "Bobcat/Doosan", doosan.model_expr, "L7-00116")
        R.check("a Doosan rule still resolves its model", bool(hits))
        # Every Doosan rule migrated as `always` (no serial on the sheet), so a
        # serial neither confirms nor refuses more than the sheet already said.
        R.check("no Doosan rule claims a serial range",
                all(h.fitment.condition != "range" for h in hits),
                "ranging a batch-allocated serial is meaningless")

    # --- Hyster prefix scoping -------------------------------------------
    # Kit 81 is scoped to serial prefix R177 (documented: H40-70FT).
    hits = F.kits_for(db, "Hyster", "H50FT", "R177V01515D")
    skus = {h.sku for h in F.confirmed(hits)}
    R.check("a Hyster serial in prefix R177 confirms the R177-scoped kit",
            "174-HS47F-00G05-01" in skus, f"confirmed {skus}")
    hits = F.kits_for(db, "Hyster", "H50FT", "G004V01515D")
    hs47 = [h for h in hits if h.sku == "174-HS47F-00G05-01"]
    R.check("the same model under another prefix is unconfirmed, not excluded",
            hs47 and hs47[0].verdict == F.UNCONFIRMED,
            f"got {[(h.sku, h.verdict) for h in hs47]}")
    R.check("and it says which prefix it wanted",
            hs47 and "R177" in hs47[0].reason, f"reason {hs47[0].reason if hs47 else None}")

    # --- a pattern condition is never a quiet match ----------------------
    pats = db.query(KitFitment).filter(KitFitment.condition == "pattern").all()
    R.check("the pattern conditions survived the migration", len(pats) >= 9,
            f"found {len(pats)}")
    for fit in pats:
        R.check(f"pattern rule {fit.kit.sku}/{fit.model_expr} answers None",
                fit.matches_serial("00003") is None)
        R.check(f"pattern rule {fit.kit.sku}/{fit.model_expr} keeps its text",
                bool(fit.serial_pattern))

    # --- the rows themselves are coherent --------------------------------
    bad_always = [f for f in db.query(KitFitment).filter(
        KitFitment.condition == "always").all()
        if f.serial_lo is not None or f.serial_hi is not None or f.serial_pattern]
    R.check("no `always` rule carries a serial bound", not bad_always,
            f"{[(f.kit_id, f.model_expr) for f in bad_always]}")
    bad_range = [f for f in db.query(KitFitment).filter(
        KitFitment.condition == "range").all()
        if f.serial_lo is None and f.serial_hi is None and not f.serial_scope]
    R.check("no `range` rule is entirely unbounded", not bad_range,
            f"{[(f.kit_id, f.model_expr) for f in bad_range]}")
    unordered = [f for f in db.query(KitFitment).filter(
        KitFitment.condition == "range").all()
        if not __import__("app.serials", fromlist=["x"]).scheme_is_ordered(f.manufacturer)]
    R.check("no `range` rule sits on an OEM with no ordered scheme", not unordered,
            f"{[(f.kit_id, f.manufacturer, f.model_expr) for f in unordered]}")

    # Every rule must name a kit that exists.
    orphans = [f for f in db.query(KitFitment).all() if f.kit is None]
    R.check("no fitment rule is orphaned", not orphans)

    # --- the default kit-list grouping -----------------------------------
    grouped = F.kits_by_manufacturer(db)
    R.check("kits group under at least a few OEMs", len(grouped) >= 5,
            f"OEMs: {sorted(grouped)}")
    R.check("a kit naming a twin brand appears under both",
            any("174-HS47F-00G05-01" in [k.sku for k in grouped.get(oem, [])]
                for oem in ("Hyster",))
            and any("174-HS47F-00G05-01" in [k.sku for k in grouped.get(oem, [])]
                    for oem in ("Yale",)),
            "kit 81 names Hyster models and their Yale twins")
    R.check("no OEM group lists a kit twice",
            all(len(ks) == len({k.sku for k in ks}) for ks in grouped.values()))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from app.database import SessionLocal  # noqa: E402
    from stress_lookup import R  # noqa: E402

    db = SessionLocal()
    try:
        run(R, db)
    finally:
        db.close()
    raise SystemExit(R.summary())
