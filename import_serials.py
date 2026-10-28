"""Import order management's serials, then rebuild the observed ranges.

    .venv/Scripts/python.exe import_serials.py orders.csv              # preview
    .venv/Scripts/python.exe import_serials.py orders.csv --apply
    .venv/Scripts/python.exe import_serials.py --derive-only           # no file

Dry run by default: it prints the column mapping it worked out, every row it
would skip and why, and the ranges the import would move -- and writes nothing.

Headers are matched by meaning, not position, so order management's own export
should work unchanged. `--show-mapping` prints what matched if it looks wrong.
The required columns are part number, OEM, model and serial; customer, location,
order reference and a note are kept when present.

A part number not in the kit catalog, and a serial that cannot be placed in its
OEM's scheme, are both WARNINGS, not errors -- the record is still written. The
fact is order management's; our ability to interpret it is ours, and throwing the
row away loses the only copy.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import fitment_derive, serial_intake  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="the CSV from order management")
    ap.add_argument("--apply", action="store_true", help="write the records")
    ap.add_argument("--derive-only", action="store_true",
                    help="skip the import and just rebuild the observed ranges")
    ap.add_argument("--recorded-by", default=None, help="who to credit")
    ap.add_argument("--show-mapping", action="store_true",
                    help="print the column mapping and stop")
    args = ap.parse_args()

    if not args.csv and not args.derive_only:
        ap.error("give a CSV, or --derive-only")

    init_db()
    db = SessionLocal()
    try:
        if args.csv:
            path = Path(args.csv)
            if not path.exists():
                print(f"no such file: {path}")
                return 2
            report = serial_intake.read_csv_file(db, path)

            print(f"{path.name}: {len(report.rows)} data rows")
            print("columns matched:")
            for header, field_name in report.mapping.items():
                print(f"    {header!r:<28} -> {field_name}")
            if report.unmapped:
                print(f"columns ignored: {report.unmapped}")
            if report.missing_fields:
                print(f"\nCANNOT IMPORT -- no column for: "
                      f"{', '.join(report.missing_fields)}")
                return 1
            if args.show_mapping:
                return 0

            print(f"\nto write:   {len(report.writable)}")
            print(f"duplicates: {len(report.duplicates)} (already on record)")
            print(f"rejected:   {len(report.failed)}")
            for row in report.failed:
                print(f"    line {row.line}: {'; '.join(row.errors)}")
            warned = [r for r in report.rows if r.warnings]
            if warned:
                print(f"\nwritten with a warning ({len(warned)}):")
                for row in warned[:25]:
                    print(f"    line {row.line} {row.data.get('serial','?'):<16} "
                          f"{'; '.join(row.warnings)}")
                if len(warned) > 25:
                    print(f"    ... and {len(warned) - 25} more")

            if args.apply:
                written = serial_intake.commit(db, report, args.recorded_by)
                print(f"\nwrote {len(written)} serial records.")
            else:
                print("\nDRY RUN -- no records written.")

        # The derivation reads what is committed, so on a dry run it reports the
        # ranges as they stand, not as the import would leave them.
        print("\n" + "-" * 68)
        print("observed ranges" + ("" if args.apply or args.derive_only
                                   else " (as they stand -- import not applied)"))
        print("-" * 68)
        derived = (fitment_derive.apply(db) if (args.apply or args.derive_only)
                   else fitment_derive.analyse(db))
        for line in fitment_derive.describe(db, derived):
            print(line)
        if args.apply or args.derive_only:
            print(f"\nrebuilt {derived.written} observed rows "
                  f"(replaced {derived.removed}).")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
