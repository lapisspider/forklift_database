"""Spec-sheet-first data pass: capacity (kg), fuel, series, and OSHA class.

For each forklift it reads the ACTUAL spec sheet (pdf_url, else source_url) via
Tavily extract and asks Claude for the printed capacity + fuel + series + class,
with a confidence flag and whether the sheet really covers that model. If there is
no usable sheet (or it doesn't cover the model), it falls back to a Tavily web
answer-search. High-confidence values are applied directly; everything is written to
spec_pass_report.csv for review.

Capacity rounding (per the user's rule):
  - value printed in kg  -> use as-is (already a clean rating).
  - value printed only in lb -> convert and FLOOR to the nearest 100 kg.

Usage:
  python spec_pass.py                 # all forklifts except BYD (default)
  python spec_pass.py --oem Toyota    # one brand
  python spec_pass.py --include-byd   # don't skip BYD
"""
from __future__ import annotations

import argparse
import csv
import time

from anthropic import Anthropic

from app.ai import tavily_client
from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

REPORT = "spec_pass_report.csv"
CLASSES = {"Class I", "Class II", "Class III", "Class IV", "Class V", "Class VI", "Class VII"}
_FUEL_FIX = {
    "electric": "Electric", "battery": "Electric", "diesel": "Diesel",
    "lpg": "LPG", "lp": "LPG", "lp gas": "LPG", "propane": "LPG",
    "liquefied petroleum": "LPG", "gasoline": "Gasoline", "gas": "Gasoline",
    "gasoline/lpg": "Gasoline/LPG", "diesel/lpg": "Diesel/LPG",
    "dual fuel": "Gasoline/LPG",
}
FUELS = {"Electric", "Diesel", "LPG", "Gasoline", "Gasoline/LPG", "Diesel/LPG"}


def _norm_fuel(v):
    if not v:
        return None
    return _FUEL_FIX.get(v.strip().lower(), v.strip())


def _to_kg(value, unit):
    """kg as printed -> as-is; lb-only -> floor to nearest 100 kg."""
    if value in (None, ""):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").strip().lower()
    if u == "kg":
        return int(round(value))
    if u in ("lb", "lbs", "pound", "pounds"):
        return int((value * 0.453592) // 100 * 100)
    return None


def _verbose_series(s: str) -> bool:
    s2 = s.lower()
    return ("(" in s) or ("lb" in s2) or ("," in s) or len(s) > 38


_CLASS_RULES = (
    "OSHA classes: I=electric rider counterbalance (cushion OR pneumatic); "
    "II=electric narrow-aisle (reach/order-picker/turret ONLY, never counterbalance); "
    "III=electric hand/walkie (pallet jack, walkie stacker); IV=IC cushion tire; "
    "V=IC pneumatic tire; VI=electric/IC tow tractor; VII=rough-terrain."
)

_TOOL = {
    "name": "record",
    "description": "Record spec-sheet-derived data for the model.",
    "input_schema": {
        "type": "object",
        "properties": {
            "covers_model": {"type": "boolean",
                             "description": "true only if the provided text actually specifies THIS exact model"},
            "capacity_value": {"type": ["number", "null"],
                               "description": "rated load capacity as PRINTED (do not convert)"},
            "capacity_unit": {"type": ["string", "null"], "enum": ["kg", "lb", None]},
            "fuel_type": {"type": ["string", "null"],
                          "description": "Electric/Diesel/LPG/Gasoline/Gasoline-LPG/Diesel-LPG; null if not stated"},
            "series": {"type": ["string", "null"],
                       "description": "official series/family name for THIS OEM; null if not stated"},
            "truck_class": {"type": ["string", "null"], "enum": list(CLASSES) + [None]},
            "confident": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["covers_model", "confident"],
    },
}


def _extract(client, fk, context, source):
    prompt = (
        f"OEM: {fk.manufacturer}\nModel: {fk.model}\n{_CLASS_RULES}\n\n"
        f"Source ({source}) text:\n{context or '(none)'}\n\n"
        "Return the rated load capacity exactly as PRINTED (capacity_value + "
        "capacity_unit kg or lb; do not convert), the fuel type, the official series "
        "name for this OEM, and the OSHA class. Set covers_model=false if the text "
        "does not actually specify this exact model. Set confident=true only when the "
        "values are well-supported for THIS model."
    )
    resp = client.messages.create(
        model=settings.claude_model, max_tokens=500,
        tools=[_TOOL], tool_choice={"type": "tool", "name": "record"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record":
            d = dict(block.input)
            d["source"] = source
            return d
    return {"covers_model": False, "confident": False, "source": source}


def process(client, fk, cache):
    doc_url = fk.pdf_url or fk.source_url
    res = None
    if doc_url:
        if doc_url not in cache:
            try:
                cache[doc_url] = tavily_client.extract_url(doc_url)[:20_000]
            except Exception:  # noqa: BLE001
                cache[doc_url] = ""
        text = cache[doc_url]
        if text.strip():
            res = _extract(client, fk, text, "sheet")
            if not res.get("covers_model"):
                res = None
    if res is None:  # web fallback
        try:
            s = tavily_client.answer_search(
                f"{fk.manufacturer} {fk.model} forklift rated load capacity, fuel type, series")
            ans = s.get("answer") or ""
            content = (("AI ANSWER:\n" + ans + "\n\n") if ans else "") + "\n\n".join(
                (r.get("raw_content") or r.get("content") or "") for r in s.get("results", [])[:3])
            res = _extract(client, fk, content[:14_000], "web")
        except Exception as e:  # noqa: BLE001
            res = {"covers_model": False, "confident": False, "source": "error", "notes": str(e)}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oem", default=None)
    ap.add_argument("--include-byd", action="store_true")
    ap.add_argument("--resume", action="store_true",
                    help="skip forklifts already in spec_pass_report.csv and append")
    ap.add_argument("--sleep", type=float, default=0.4)
    args = ap.parse_args()
    if not settings.web_lookup_enabled:
        raise SystemExit("ANTHROPIC_API_KEY and TAVILY_API_KEY must be set.")

    db = SessionLocal()
    q = db.query(Forklift)
    if args.oem:
        q = q.filter(Forklift.manufacturer.ilike(args.oem))
    elif not args.include_byd:
        q = q.filter(Forklift.manufacturer != "BYD")
    fks = q.order_by(Forklift.manufacturer, Forklift.model).all()

    fields = ["id", "manufacturer", "model", "source", "covers", "confident",
              "cap_old", "cap_new", "cap_raw", "fuel_old", "fuel_new",
              "series_old", "series_new", "class_old", "class_new", "applied", "notes"]
    done_ids: set[int] = set()
    if args.resume:
        import os
        if os.path.exists(REPORT):
            for r in csv.DictReader(open(REPORT, encoding="utf-8")):
                if r.get("source") in ("sheet", "web"):
                    done_ids.add(int(r["id"]))
        fks = [f for f in fks if f.id not in done_ids]
        print(f"Resuming: {len(done_ids)} already done, {len(fks)} remaining ...")
    else:
        print(f"Spec-sheet pass over {len(fks)} forklifts ...")

    client = Anthropic(api_key=settings.anthropic_api_key)
    cache: dict[str, str] = {}
    applied = {"capacity": 0, "fuel": 0, "series": 0, "class": 0}
    mode = "a" if (args.resume and done_ids) else "w"
    with open(REPORT, mode, newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if mode == "w":
            w.writeheader()
        for i, fk in enumerate(fks, 1):
            try:
                r = process(client, fk, cache)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(fks)}] {fk.manufacturer} {fk.model}: {e}")
                continue
            conf = bool(r.get("confident"))
            cap_new = _to_kg(r.get("capacity_value"), r.get("capacity_unit"))
            fuel_new = _norm_fuel(r.get("fuel_type"))
            series_new = (r.get("series") or "").strip()
            class_new = (r.get("truck_class") or "").strip()
            did = []
            if conf and cap_new and cap_new != fk.capacity_kg:
                fk.capacity_kg = cap_new; applied["capacity"] += 1; did.append("capacity")
            if conf and fuel_new in FUELS and fuel_new != fk.fuel_type:
                fk.fuel_type = fuel_new; applied["fuel"] += 1; did.append("fuel")
            if conf and series_new and not _verbose_series(series_new) and series_new != (fk.series or ""):
                fk.series = series_new; applied["series"] += 1; did.append("series")
            if conf and class_new in CLASSES and class_new != (fk.truck_class or ""):
                fk.truck_class = class_new; applied["class"] += 1; did.append("class")
            w.writerow({
                "id": fk.id, "manufacturer": fk.manufacturer, "model": fk.model,
                "source": r.get("source"), "covers": r.get("covers_model"), "confident": conf,
                "cap_old": fk.capacity_kg if "capacity" not in did else "", "cap_new": cap_new or "",
                "cap_raw": f"{r.get('capacity_value')} {r.get('capacity_unit')}",
                "fuel_old": fk.fuel_type if "fuel" not in did else "", "fuel_new": fuel_new or "",
                "series_old": fk.series if "series" not in did else "", "series_new": series_new,
                "class_old": fk.truck_class if "class" not in did else "", "class_new": class_new,
                "applied": ",".join(did), "notes": (r.get("notes") or "")[:120],
            })
            if i % 10 == 0:
                db.commit(); fh.flush()
                print(f"  [{i}/{len(fks)}] applied so far: {applied}")
            if args.sleep:
                time.sleep(args.sleep)
        db.commit()
    db.close()
    print(f"\nDone. Applied high-confidence: {applied}. Report: {REPORT}")


if __name__ == "__main__":
    main()
