"""Re-examine ONLY the low-confidence series rows and correct them.

No web calls. Reads series_backfill_report.csv, takes the rows flagged
confident=False, and for each OEM asks Claude (using its own knowledge) to give
the correct, CONSISTENT manufacturer series for those models — fixing wrong
cross-family tags (e.g. Hyster ERC electric reach trucks mislabeled "Fortis")
and merging variants (VX Series / Veracitor VX). If a model's series genuinely
cannot be determined, Claude returns "<REVIEW>" and we leave it unchanged and
list it. Applies corrections and writes fix_series_report.csv.

Usage:  python fix_lowconf_series.py
"""
from __future__ import annotations

import csv
from collections import defaultdict

from anthropic import Anthropic

from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

IN_REPORT = "series_backfill_report.csv"
OUT_REPORT = "fix_series_report.csv"


def corrected_for_oem(oem: str, items: list[dict]) -> list[dict]:
    """items: [{model, current}] -> [{model, series, review}]."""
    client = Anthropic(api_key=settings.anthropic_api_key)
    tool = {
        "name": "record",
        "description": "Corrected series per model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "series": {"type": "string", "description":
                                       "correct manufacturer series, or '<REVIEW>' "
                                       "if genuinely unknown"},
                        },
                        "required": ["model", "series"],
                    },
                }
            },
            "required": ["results"],
        },
    }
    listing = "\n".join(f"- {it['model']}  (currently: {it['current']})" for it in items)
    prompt = (
        f"OEM: {oem}\n\n"
        "These models have LOW-CONFIDENCE series values that may be wrong or "
        "inconsistent. Using your knowledge of this manufacturer's actual lineup, "
        "give the CORRECT series/family name for each model. Requirements:\n"
        "- Fix wrong cross-family tags (e.g. a Hyster ERC electric reach truck is "
        "NOT 'Fortis'; Fortis is the IC/FT line).\n"
        "- Be CONSISTENT: models in the same real family get the identical series "
        "string (merge variants like 'VX Series' and 'Veracitor VX' to one).\n"
        "- Keep names concise (family name + 'Series' where natural).\n"
        "- If you genuinely cannot determine a model's series, return '<REVIEW>'.\n\n"
        f"Models:\n{listing}"
    )
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=3000,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input.get("results", []) or []
    return []


def main() -> None:
    if not settings.ai_enabled:
        raise SystemExit("ANTHROPIC_API_KEY must be set in .env.")

    rows = list(csv.DictReader(open(IN_REPORT, encoding="utf-8")))
    low = [r for r in rows if r.get("confident") == "False"]
    by_oem: dict[str, list[dict]] = defaultdict(list)
    for r in low:
        by_oem[r["oem"]].append({"model": r["model"], "current": r["new_series"]})

    db = SessionLocal()
    changed, review = 0, []
    with open(OUT_REPORT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["oem", "model", "old_series", "new_series", "status"])
        for oem, items in by_oem.items():
            # de-dupe models within the OEM batch
            seen, uniq = set(), []
            for it in items:
                if it["model"] not in seen:
                    seen.add(it["model"]); uniq.append(it)
            results = corrected_for_oem(oem, uniq)
            rmap = {r["model"].strip().lower(): (r.get("series") or "").strip()
                    for r in results}
            for it in uniq:
                fk = (db.query(Forklift)
                      .filter(Forklift.manufacturer == oem,
                              Forklift.model == it["model"]).first())
                if not fk:
                    continue
                new = rmap.get(it["model"].strip().lower(), "")
                if not new or new == "<REVIEW>":
                    review.append(f"{oem} {it['model']} (currently {fk.series!r})")
                    w.writerow([oem, it["model"], fk.series, "", "REVIEW"])
                    continue
                if new != fk.series:
                    w.writerow([oem, it["model"], fk.series, new, "fixed"])
                    fk.series = new
                    changed += 1
                else:
                    w.writerow([oem, it["model"], fk.series, new, "unchanged"])
            db.commit()
            print(f"  {oem}: {len(uniq)} reviewed")
    db.close()
    print(f"\nDone. Corrected {changed}. {len(review)} need manual review. -> {OUT_REPORT}")
    for r in review:
        print("  REVIEW:", r)


if __name__ == "__main__":
    main()
