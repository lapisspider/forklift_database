"""Backfill/repair the `series` field by WEB-SEARCHING the real manufacturer series.

Series = the manufacturer's OFFICIAL published series/family NAME a model belongs
to (e.g. Hyster ESC030AD -> "ESC AD" stacker series; Toyota 8FGCU25 -> "8-Series"),
NOT the company's internal model-number range shorthand.

Method: group forklifts by their current (manufacturer, series). For each group,
run ONE Tavily web search for a representative model, then ask Claude to read the
web content and return each model's official series (splitting the group if its
models are actually different families). Every result records its source URL and a
confidence flag; low-confidence rows (no reliable source) are flagged for review.

Usage:
  python backfill_series.py --oem Hyster    # one OEM (recommended: verify first)
  python backfill_series.py                 # all OEMs
  python backfill_series.py --list          # groups that would be processed
"""
from __future__ import annotations

import argparse
import csv
import time
from collections import defaultdict

from anthropic import Anthropic

from app.ai import tavily_client
from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

REPORT = "series_backfill_report.csv"


def group_key(fk: Forklift) -> tuple[str, str]:
    return (fk.manufacturer, fk.series or "")


def resolve_group(oem: str, models: list[str]) -> tuple[list[dict], str]:
    """Web-search a representative model, then have Claude name each model's
    official series from the web content. Returns (results, source_url)."""
    rep = max(models, key=len)  # a fuller model string searches better
    search = tavily_client.search_spec_sheet(f"{oem} {rep} series")
    results = search.get("results", [])
    source_url = results[0].get("url") if results else ""
    content = "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )[:20000]

    client = Anthropic(api_key=settings.anthropic_api_key)
    tool = {
        "name": "record_series",
        "description": "Record each model's official manufacturer series.",
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
                                       "manufacturer's OFFICIAL published series/family "
                                       "name, not a model-number range"},
                            "confident": {"type": "boolean", "description":
                                          "true only if the web content clearly supports it"},
                        },
                        "required": ["model", "series", "confident"],
                    },
                }
            },
            "required": ["results"],
        },
    }
    prompt = (
        f"OEM: {oem}\n"
        f"Models to classify: {', '.join(models)}\n\n"
        f"Web search results:\n{content or '(no content retrieved)'}\n\n"
        "For each model, return the manufacturer's OFFICIAL published series/family "
        "NAME it belongs to (the product-line name the manufacturer markets, e.g. "
        "Hyster 'ESC AD' stackers, 'Fortis', 'XT'; NOT a raw model-number range). "
        "If different models here belong to different real families, give each its "
        "own. Set confident=false when the web content does not clearly support the "
        "series (including if a model looks like it may not be a real product)."
    )
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=2000,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_series"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            res = block.input.get("results", [])
            return (res if isinstance(res, list) else []), source_url
    return [], source_url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oem", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--missing-only", action="store_true",
                    help="only forklifts that currently have no series")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    if not settings.web_lookup_enabled:
        raise SystemExit("ANTHROPIC_API_KEY and TAVILY_API_KEY must be set in .env.")

    db = SessionLocal()
    q = db.query(Forklift)
    if args.oem:
        q = q.filter(Forklift.manufacturer.ilike(args.oem))
    if args.missing_only:
        q = q.filter(Forklift.series.is_(None))

    groups: dict[tuple[str, str], list[Forklift]] = defaultdict(list)
    for fk in q.all():
        groups[group_key(fk)].append(fk)

    if args.list:
        for (oem, series), fks in sorted(groups.items()):
            print(f"{len(fks):3}  {oem}  [{series or 'no series'}]")
        print(f"\n{len(groups)} groups.")
        db.close()
        return

    print(f"Processing {len(groups)} family group(s)"
          + (f" for '{args.oem}'" if args.oem else "") + " ...")
    import os
    new_file = not os.path.exists(REPORT)
    with open(REPORT, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["oem", "model", "old_series", "new_series", "source_url", "confident"])
        set_count, low = 0, 0
        for i, ((oem, old_series), fks) in enumerate(sorted(groups.items()), 1):
            models = [f.model for f in fks]
            index = {m.strip().lower(): f for m, f in zip(models, fks)}
            try:
                results, source = resolve_group(oem, models)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(groups)}] {oem} [{old_series}]: error {e}")
                continue
            got = 0
            for r in results:
                series = (r.get("series") or "").strip()
                fk = index.get((r.get("model") or "").strip().lower())
                if not fk or not series:
                    continue
                fk.series = series
                got += 1
                set_count += 1
                conf = bool(r.get("confident"))
                if not conf:
                    low += 1
                writer.writerow([oem, fk.model, old_series, series, source, conf])
            db.commit()
            fh.flush()
            print(f"  [{i}/{len(groups)}] {oem} [{old_series}] -> set {got}/{len(fks)}")
            if args.sleep and i < len(groups):
                time.sleep(args.sleep)

    db.close()
    print(f"Done. Set {set_count} series ({low} low-confidence). Review: {REPORT}")


if __name__ == "__main__":
    main()
