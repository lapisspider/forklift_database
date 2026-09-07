"""Audit forklift records: assign OSHA class + fact-check series/capacity/fuel/year.

Groups forklifts by (manufacturer, series) and runs ONE Tavily answer-search + ONE
Claude call per family. For each model it returns a verified series, an OSHA class,
and best-effort corrections/estimates for capacity, fuel, and production years.

Two-phase, report-first (the user reviews before overwriting existing data):

  1. DEFAULT RUN  ->  writes forklift_audit_report.csv AND applies the OSHA `class`
     directly (a new, empty field — nothing is overwritten). Series/capacity/fuel/year
     are written as PROPOSALS only; they are NOT applied.
  2. APPLY RUN    ->  `python audit_forklifts.py --apply forklift_audit_report.csv`
     applies the proposed series/capacity/fuel/year corrections ONLY for rows the user
     marked apply=yes in the CSV.

Usage:
  python audit_forklifts.py --oem BYD        # audit one brand first (recommended)
  python audit_forklifts.py --list           # show family groups
  python audit_forklifts.py                   # audit all
  python audit_forklifts.py --apply forklift_audit_report.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import time
from collections import defaultdict

from anthropic import Anthropic

from app.ai import tavily_client
from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

REPORT = "forklift_audit_report.csv"
FIELDS = ["id", "manufacturer", "model", "current_series", "proposed_series",
          "proposed_class", "proposed_capacity_kg", "proposed_fuel",
          "proposed_year_start", "proposed_year_end", "confident", "issues", "apply"]

CLASSES = {"Class I", "Class II", "Class III", "Class IV", "Class V", "Class VI", "Class VII"}
FUELS = {"Electric", "Diesel", "LPG", "Gasoline", "Gasoline/LPG", "Diesel/LPG"}
_FUEL_FIX = {
    "electric": "Electric", "battery": "Electric", "diesel": "Diesel",
    "lpg": "LPG", "lp": "LPG", "lp gas": "LPG", "propane": "LPG",
    "gasoline": "Gasoline", "gas": "Gasoline",
    "gasoline/lpg": "Gasoline/LPG", "diesel/lpg": "Diesel/LPG",
}


def _norm_fuel(v: str | None) -> str | None:
    if not v:
        return None
    return _FUEL_FIX.get(v.strip().lower(), v.strip())


_CLASS_RULES = (
    "OSHA classes: I=electric rider (counterbalance, cushion OR pneumatic tire); "
    "II=electric narrow-aisle (reach/order-picker/turret ONLY — never a counterbalance); "
    "III=electric hand/walkie (pallet jack, walkie stacker); IV=IC cushion (solid) tire; "
    "V=IC pneumatic tire; VI=electric/IC tow tractor; VII=rough-terrain. "
    "An electric counterbalance is Class I even with cushion tires; cushion tires only "
    "matter for distinguishing Class IV from V (internal-combustion trucks)."
)

_TOOL = {
    "name": "record_audit",
    "description": "Record the audit result for each model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "model": {"type": "string"},
                        "series": {"type": ["string", "null"],
                                   "description": "verified OFFICIAL series/family name for THIS OEM"},
                        "truck_class": {"type": ["string", "null"],
                                        "enum": list(CLASSES) + [None]},
                        "capacity_kg": {"type": ["number", "null"],
                                        "description": "corrected capacity ONLY if current looks wrong; else null"},
                        "fuel_type": {"type": ["string", "null"],
                                      "description": "corrected canonical fuel ONLY if current looks wrong; else null"},
                        "year_start": {"type": ["integer", "null"]},
                        "year_end": {"type": ["integer", "null"]},
                        "confident": {"type": "boolean"},
                        "issues": {"type": "string",
                                   "description": "short note of any problem: wrong series, rebadge/conflation, mismatched fuel, etc. Empty if fine."},
                    },
                    "required": ["model", "confident"],
                },
            }
        },
        "required": ["results"],
    },
}


def audit_family(oem: str, fks: list[Forklift]) -> list[dict]:
    rep = max((f.model for f in fks), key=len)
    query = (f"{oem} {rep} forklift specifications: series/family name, load capacity, "
             f"fuel type, production years, electric vs internal combustion, tire type")
    search = tavily_client.answer_search(query)
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:18_000]

    current = "\n".join(
        f"- {f.model} | series={f.series!r} | capacity_kg={f.capacity_kg} | "
        f"fuel={f.fuel_type!r} | years={f.year_start}-{f.year_end} | class={f.truck_class!r}"
        for f in fks
    )
    prompt = (
        f"OEM: {oem}\n{_CLASS_RULES}\n\n"
        f"Current database rows to audit:\n{current}\n\n"
        f"Web search results:\n{content or '(no content retrieved)'}\n\n"
        "For EACH model above, return:\n"
        "- series: the manufacturer's OFFICIAL series/family name for THIS OEM (not a "
        "rebadge from a different brand; not a raw model-number range). If the current "
        "series looks wrong or belongs to another brand's equivalent, correct it.\n"
        "- truck_class: best-fit OSHA class 'Class I'..'Class VII' (infer tire type from "
        "the model line if unstated).\n"
        "- capacity_kg / fuel_type: ONLY if the current value looks wrong (else null). "
        "fuel_type must be canonical: Electric, Diesel, LPG, Gasoline, Gasoline/LPG, Diesel/LPG.\n"
        "- year_start/year_end: correct or best-effort estimate.\n"
        "- confident: true if well-supported.\n"
        "- issues: flag rebadges, conflated OEM/model strings, or clearly wrong data. Empty if fine."
    )
    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.claude_model, max_tokens=3000,
        tools=[_TOOL], tool_choice={"type": "tool", "name": "record_audit"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_audit":
            res = block.input.get("results", [])
            return res if isinstance(res, list) else []
    return []


def run_audit(args) -> None:
    db = SessionLocal()
    q = db.query(Forklift)
    if args.oem:
        q = q.filter(Forklift.manufacturer.ilike(args.oem))
    groups: dict[tuple[str, str], list[Forklift]] = defaultdict(list)
    for fk in q.all():
        groups[(fk.manufacturer, fk.series or "")].append(fk)

    if args.list:
        for (oem, series), fks in sorted(groups.items()):
            print(f"{len(fks):3}  {oem}  [{series or 'no series'}]")
        print(f"\n{len(groups)} family groups.")
        db.close()
        return

    print(f"Auditing {len(groups)} family group(s)"
          + (f" for '{args.oem}'" if args.oem else "") + " ...")
    class_set, flagged = 0, 0
    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for i, ((oem, series), fks) in enumerate(sorted(groups.items()), 1):
            index = {f.model.strip().lower(): f for f in fks}
            try:
                results = audit_family(oem, fks)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(groups)}] {oem} [{series}]: error {e}")
                continue
            got = 0
            for r in results:
                fk = index.get((r.get("model") or "").strip().lower())
                if not fk:
                    continue
                got += 1
                tc = (r.get("truck_class") or "").strip()
                if tc in CLASSES:            # apply class directly (new field)
                    fk.truck_class = tc
                    class_set += 1
                new_series = (r.get("series") or "").strip()
                new_fuel = _norm_fuel(r.get("fuel_type"))
                issues = (r.get("issues") or "").strip()
                if issues:
                    flagged += 1
                w.writerow({
                    "id": fk.id, "manufacturer": fk.manufacturer, "model": fk.model,
                    "current_series": fk.series or "",
                    "proposed_series": new_series if new_series and new_series != (fk.series or "") else "",
                    "proposed_class": tc,
                    "proposed_capacity_kg": r.get("capacity_kg") if r.get("capacity_kg") is not None else "",
                    "proposed_fuel": new_fuel if new_fuel and new_fuel != (fk.fuel_type or "") else "",
                    "proposed_year_start": r.get("year_start") if r.get("year_start") is not None else "",
                    "proposed_year_end": r.get("year_end") if r.get("year_end") is not None else "",
                    "confident": bool(r.get("confident")),
                    "issues": issues, "apply": "",
                })
            db.commit()
            fh.flush()
            print(f"  [{i}/{len(groups)}] {oem} [{series}] -> {got}/{len(fks)} audited")
            if args.sleep and i < len(groups):
                time.sleep(args.sleep)
    db.close()
    print(f"\nDone. Applied class to {class_set} rows; flagged {flagged} with issues.")
    print(f"Review {REPORT}, mark rows apply=yes, then: python audit_forklifts.py --apply {REPORT}")


def apply_report(path: str) -> None:
    if not os.path.exists(path):
        raise SystemExit(f"Report not found: {path}")
    db = SessionLocal()
    applied = 0
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("apply") or "").strip().lower() not in {"yes", "y", "true", "1"}:
                continue
            fk = db.get(Forklift, int(row["id"]))
            if not fk:
                continue
            if row.get("proposed_series"):
                fk.series = row["proposed_series"].strip()
            if row.get("proposed_fuel"):
                fk.fuel_type = row["proposed_fuel"].strip()
            if row.get("proposed_capacity_kg"):
                try:
                    fk.capacity_kg = float(row["proposed_capacity_kg"])
                except ValueError:
                    pass
            if row.get("proposed_year_start"):
                try:
                    fk.year_start = int(row["proposed_year_start"])
                except ValueError:
                    pass
            if row.get("proposed_year_end"):
                try:
                    fk.year_end = int(row["proposed_year_end"])
                except ValueError:
                    pass
            applied += 1
    db.commit()
    db.close()
    print(f"Applied approved corrections to {applied} row(s).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oem", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--apply", metavar="CSV", default=None,
                    help="apply approved (apply=yes) rows from a report CSV")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    if args.apply:
        apply_report(args.apply)
        return
    if not settings.web_lookup_enabled:
        raise SystemExit("ANTHROPIC_API_KEY and TAVILY_API_KEY must be set in .env.")
    run_audit(args)


if __name__ == "__main__":
    main()
