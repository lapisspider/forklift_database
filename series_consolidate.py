"""Propose (and optionally apply) a consistent series-naming scheme.

The spec-sheet pass fragmented series into per-model ranges. This groups each
manufacturer's models by their current series, asks Claude to consolidate them
into consistent family names, and writes a review CSV (proposed_series_mapping.csv).

Rules given to the model:
  - The value must NOT contain the word "Series" (the column is already 'Series').
  - Merge fragments/near-duplicates that are clearly the same family
    (e.g. 'ETG' + 'ETG 214-318' + 'ETG 230-355' -> 'ETG'; 'E-Series' + 'E' -> 'E').
  - KEEP genuinely distinct sub-families separate (ERC vs ERP; reach vs counterbalance).
  - Use the manufacturer's real product-family name; normalize dashes/case/spacing.

Report-first: default run only writes the CSV. Apply after review:
  python series_consolidate.py --apply proposed_series_mapping.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict

from anthropic import Anthropic

from app.ai import tavily_client  # noqa: F401  (ensures app config import path)
from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

REPORT = "proposed_series_mapping.csv"
# Judgment pass (consolidation decisions) — keep Sonnet. Override with --model.
DEFAULT_MODEL = "claude-sonnet-5"
MODEL = DEFAULT_MODEL

_TOOL = {
    "name": "map_series",
    "description": "Return the canonical series name for each current series value.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mapping": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_series": {"type": "string"},
                        "new_series": {"type": "string",
                                       "description": "canonical family name, WITHOUT the word 'Series'"},
                    },
                    "required": ["old_series", "new_series"],
                },
            }
        },
        "required": ["mapping"],
    },
}


def propose(client, oem, groups) -> dict[str, str]:
    lines = "\n".join(
        f"- {old!r}  (models: {', '.join(sorted(models)[:10])}{'…' if len(models) > 10 else ''})"
        for old, models in sorted(groups.items())
    )
    prompt = (
        f"OEM: {oem}\n\nCurrent distinct series values and their models:\n{lines}\n\n"
        "Produce a canonical series name for EACH old value. Rules:\n"
        "1. The new value MUST NOT contain the word 'Series' (drop it entirely).\n"
        "2. Merge values that are clearly the SAME family or duplicates that differ only "
        "by a range, a suffix, casing, dashes, or the word Series (e.g. 'ETG' + "
        "'ETG 214-318' + 'ETG 230-355' -> 'ETG'; 'E-Series' + 'E' -> 'E'; '9 Series' + "
        "'9-Series' -> '9').\n"
        "3. KEEP genuinely distinct MARKETED sub-families separate (e.g. ERC vs ERP; "
        "Toyota 'Core' vs '8'); do not over-merge truly different lines.\n"
        "4. BUT a raw MODEL-CODE prefix is NOT a series — map it to the marketing family. "
        "The models tell you the family: Toyota's leading generation digit is the series "
        "('7FGCU','7FDU','7F' -> '7'; '8FBEU' -> '8'; '9BRU' -> '9'). Likewise collapse "
        "capacity-range variants of one family ('E45-70XN' & 'E80-120XN' are both 'E-XN').\n"
        "5. Descriptive phrases are NOT series names ('Large IC Pneumatic', 'Stand-Up "
        "Rider', 'Three Wheel Electric Rider', 'Pantograph Reach Truck', 'X ton'): map them "
        "to the proper family inferred from the models; if none is identifiable, keep the "
        "old value.\n"
        "6. Use the manufacturer's real product-family name; normalize dashes/casing/spacing. "
        "Prefer FEWER, real family names.\n"
        "Return every old value with its new value."
    )
    resp = client.messages.create(
        model=MODEL, max_tokens=3000,
        tools=[_TOOL], tool_choice={"type": "tool", "name": "map_series"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "map_series":
            out = {}
            for m in block.input.get("mapping", []):
                if isinstance(m, dict) and m.get("old_series") and m.get("new_series"):
                    out[str(m["old_series"])] = str(m["new_series"]).strip()
            return out
    return {}


def run():
    db = SessionLocal()
    fks = [f for f in db.query(Forklift).all() if f.series]
    by_oem_series = defaultdict(lambda: defaultdict(list))
    for f in fks:
        by_oem_series[f.manufacturer][f.series].append(f.model)

    client = Anthropic(api_key=settings.anthropic_api_key)
    rows = []
    for oem in sorted(by_oem_series):
        groups = by_oem_series[oem]
        try:
            mapping = propose(client, oem, groups)
        except Exception as e:  # noqa: BLE001
            print(f"  {oem}: error {e}")
            mapping = {}
        for old, models in sorted(groups.items()):
            new = mapping.get(old, "")
            rows.append({
                "manufacturer": oem, "old_series": old, "count": len(models),
                "new_series": new if new and new != old else ("" if new == old else new),
                "changed": "yes" if (new and new != old) else "",
                "example_models": ", ".join(sorted(models)[:5]),
            })
        changed = sum(1 for r in rows if r["manufacturer"] == oem and r["changed"])
        print(f"  {oem}: {len(groups)} series -> {changed} proposed changes")

    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["manufacturer", "old_series", "count",
                                           "new_series", "changed", "example_models"])
        w.writeheader(); w.writerows(rows)
    distinct_after = len({(r["manufacturer"], r["new_series"] or r["old_series"]) for r in rows})
    print(f"\nWrote {REPORT}: {len(rows)} current values -> ~{distinct_after} distinct after.")
    print(f"Review it, then: python series_consolidate.py --apply {REPORT}")
    db.close()


def apply(path):
    db = SessionLocal()
    n = 0
    for r in csv.DictReader(open(path, encoding="utf-8")):
        new = (r.get("new_series") or "").strip()
        if not new or new == r["old_series"]:
            continue
        q = db.query(Forklift).filter(Forklift.manufacturer == r["manufacturer"],
                                      Forklift.series == r["old_series"])
        n += q.update({"series": new}, synchronize_session=False)
    db.commit()
    db.close()
    print(f"Applied series consolidation to {n} forklift rows.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", metavar="CSV", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"Claude model (default {DEFAULT_MODEL}; judgment task, keep Sonnet)")
    args = ap.parse_args()
    global MODEL
    MODEL = args.model
    if args.apply:
        apply(args.apply)
    else:
        if not settings.web_lookup_enabled:
            raise SystemExit("ANTHROPIC_API_KEY must be set.")
        run()


if __name__ == "__main__":
    main()
