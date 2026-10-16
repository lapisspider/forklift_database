"""Correct `truck_class` rows that contradict the catalog's own standing rules.

Dry-run by default; pass --apply to write. Idempotent -- running it twice
changes nothing the second time, because it recomputes what is wrong rather
than replaying a list of ids.

Two rules, applied in this order:

1. **Electric counterbalance is Class I whatever the tire** (CLAUDE.md). Any
   Electric row sitting in Class IV or V contradicts a rule already written
   down, so there is no judgement to make. This runs first, which is what keeps
   2EPC5000 -- Electric, cushion-coded, stored Class V -- out of rule 2: it
   belongs in Class I, not Class IV.

2. **For IC trucks the class IS the tire** (IV cushion, V pneumatic). Where
   `tire_from_model()` reads a tire off the model code and the stored class
   says the other one, the code wins. That direction was established by the
   audit: within one series the letter does not change, yet the stored class
   flips -- Cat C-series tracking fuel on 7 of 8 rows, Hyster S (a cushion
   line) stored Class V on 7 of 11, and `GC40K`/`GP40K` both stored Class IV
   when GC/GP is exactly the cushion/pneumatic split.

Only OEMs the parser covers are touched, and only rows already in Class IV/V.
Nothing else is inferred.

After applying, refresh the seed so it reaches production (CLAUDE.md):
    cp data/forklifts.db deploy/seed.db
    then drop the users table from the copy
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai.tire import CUSHION, PNEUMATIC, tire_from_model  # noqa: E402

DB = Path(__file__).parent / "data" / "forklifts.db"
IC_CLASS = {CUSHION: "Class IV", PNEUMATIC: "Class V"}


def corrections(conn: sqlite3.Connection) -> list[tuple[int, str, str, str, str, str]]:
    """(id, oem, model, from_class, to_class, why) for every row that is wrong."""
    out = []
    rows = conn.execute(
        "SELECT id, manufacturer, model, fuel_type, truck_class FROM forklifts "
        "WHERE truck_class IN ('Class IV', 'Class V') ORDER BY manufacturer, model"
    ).fetchall()
    for r in rows:
        if (r["fuel_type"] or "") == "Electric":
            out.append((r["id"], r["manufacturer"], r["model"], r["truck_class"],
                        "Class I", "electric counterbalance is Class I on any tire"))
            continue
        tire = tire_from_model(r["manufacturer"], r["model"])
        if tire is None:
            continue
        want = IC_CLASS[tire]
        if want != r["truck_class"]:
            out.append((r["id"], r["manufacturer"], r["model"], r["truck_class"],
                        want, f"model code reads {tire.lower()}"))
    return out


def main() -> int:
    apply = "--apply" in sys.argv
    if not DB.exists():
        print(f"No database at {DB}")
        return 1

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    fixes = corrections(conn)

    if not fixes:
        print("Nothing to correct.")
        conn.close()
        return 0

    print(f"{len(fixes)} row(s) contradict a standing rule:\n")
    print(f"  {'id':>5}  {'OEM':<16} {'model':<14} {'from':<9} -> {'to':<9} why")
    print(f"  {'-' * 5}  {'-' * 16} {'-' * 14} {'-' * 9}    {'-' * 9} {'-' * 40}")
    for fid, oem, model, frm, to, why in fixes:
        print(f"  {fid:>5}  {oem:<16} {model:<14} {frm:<9} -> {to:<9} {why}")

    if not apply:
        print(f"\nDry run. Re-run with --apply to write these {len(fixes)} changes.")
        conn.close()
        return 0

    with conn:
        conn.executemany(
            "UPDATE forklifts SET truck_class = ? WHERE id = ?",
            [(to, fid) for fid, _o, _m, _f, to, _w in fixes],
        )
    print(f"\nApplied {len(fixes)} change(s).")

    left = corrections(conn)
    print(f"Re-checked: {len(left)} remaining contradiction(s).")
    conn.close()
    print("\nRemember to refresh deploy/seed.db so this reaches production.")
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
