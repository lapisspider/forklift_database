"""Report rows where the model code's tire disagrees with the stored OSHA class.

READ-ONLY by design. It opens the database in SQLite read-only mode and writes
nothing, because the list it produces is a question for a person, not a batch to
apply -- the same standing rule CLAUDE.md sets for the 19 capacity rows.

The check only works for Class IV and Class V, where the class IS the tire by
OSHA's definition (IV = IC cushion, V = IC pneumatic). Class I covers every
electric rider on any tire, so those rows have no ground truth to disagree with
and are reported separately as coverage, not as errors.

Run:
    .venv/Scripts/python.exe audit_tire_class.py
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.ai.tire import CUSHION, PNEUMATIC, tire_from_model  # noqa: E402

DB = Path(__file__).parent / "data" / "forklifts.db"
CLASS_TIRE = {"Class IV": CUSHION, "Class V": PNEUMATIC}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    if not DB.exists():
        print(f"No database at {DB}")
        return 1

    conn = _connect()
    rows = conn.execute(
        "SELECT id, manufacturer, series, model, fuel_type, truck_class "
        "FROM forklifts ORDER BY manufacturer, model"
    ).fetchall()
    conn.close()

    disagree: list[tuple[sqlite3.Row, str, str]] = []
    agree = 0
    no_rule_ic = 0
    parsed_non_ic = 0
    by_oem: Counter[str] = Counter()

    for r in rows:
        tire = tire_from_model(r["manufacturer"], r["model"])
        expected = CLASS_TIRE.get(r["truck_class"] or "")
        if tire:
            by_oem[r["manufacturer"]] += 1
        if expected is None:
            if tire:
                parsed_non_ic += 1
            continue
        if tire is None:
            no_rule_ic += 1
        elif tire == expected:
            agree += 1
        else:
            disagree.append((r, tire, expected))

    checkable = agree + len(disagree)
    print("=" * 78)
    print("Tire-from-model vs stored OSHA class")
    print("=" * 78)
    print(f"  Class IV/V rows with a parse rule : {checkable}")
    print(f"    agree                           : {agree}")
    print(f"    DISAGREE                        : {len(disagree)}")
    print(f"  Class IV/V rows with no rule      : {no_rule_ic}")
    print(f"  Parsed rows outside Class IV/V    : {parsed_non_ic} (no ground truth to check)")
    print()
    print("  Parser coverage by OEM:")
    for oem, n in by_oem.most_common():
        print(f"    {oem:<28} {n}")

    if not disagree:
        print("\nNo disagreements.")
        return 0

    print()
    print("=" * 78)
    print(f"{len(disagree)} disagreement(s) -- each one is a judgement call, not a fix to apply")
    print("=" * 78)
    print(f"  {'id':>5}  {'OEM':<16} {'model':<14} {'code says':<10} {'class says':<10} class")
    print(f"  {'-' * 5}  {'-' * 16} {'-' * 14} {'-' * 10} {'-' * 10} {'-' * 9}")
    for r, tire, expected in disagree:
        print(f"  {r['id']:>5}  {r['manufacturer']:<16} {r['model']:<14} "
              f"{tire:<10} {expected:<10} {r['truck_class']}")

    print()
    print("Reading the list: within one series the letter does not change, so a")
    print("series whose rows carry BOTH classes is inconsistent on its own terms --")
    print("that points at the stored class rather than the code. Check whether the")
    print("split tracks fuel instead of tire; that is the signature of a class")
    print("inferred from the wrong field.")
    print()
    print("Nothing was written. Decide per row.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
