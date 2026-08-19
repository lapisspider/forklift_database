"""Backfill production-year ranges (year_start / year_end) via web search.

Best-effort: production years are often unpublished, so the model is told to
leave years null rather than guess. Groups forklifts by (manufacturer, series),
runs one Tavily search per family, and asks Claude to read the web content and
return each model's production span. Records source URL + confidence; writes
year_backfill_report.csv.

Usage:
  python backfill_years.py --oem Toyota      # one OEM (verify first)
  python backfill_years.py --missing-only    # only forklifts with no year
  python backfill_years.py --list            # groups that would be processed
  python backfill_years.py                   # all
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

REPORT = "year_backfill_report.csv"


def resolve_group(oem: str, models: list[str]) -> tuple[list[dict], str]:
    rep = max(models, key=len)
    # Question-style query + Tavily's AI answer (like a Google AI overview),
    # which surfaces production years that aren't on any single spec page.
    query = (f"What years were the {oem} {rep} forklift models manufactured? "
             f"production start year and end year")
    search = tavily_client.answer_search(query)
    results = search.get("results", [])
    source_url = results[0].get("url") if results else ""
    answer = search.get("answer") or ""
    content = ("AI SEARCH ANSWER:\n" + answer + "\n\n" if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )[:20000]

    client = Anthropic(api_key=settings.anthropic_api_key)
    tool = {
        "name": "record_years",
        "description": "Record each model's production year span.",
        "input_schema": {
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string"},
                            "year_start": {"type": ["integer", "null"]},
                            "year_end": {"type": ["integer", "null"],
                                         "description": "null if still in production"},
                            "confident": {"type": "boolean"},
                        },
                        "required": ["model", "confident"],
                    },
                }
            },
            "required": ["results"],
        },
    }
    prompt = (
        f"OEM: {oem}\n"
        f"Models: {', '.join(models)}\n\n"
        f"Web search results:\n{content or '(no content retrieved)'}\n\n"
        "For each model, give its PRODUCTION year span: year_start (first year made) "
        "and year_end (last year made; null if still in production). The 'AI SEARCH "
        "ANSWER' above often states these years directly — use it and the content and "
        "your knowledge. Fill years whenever the sources or your knowledge reasonably "
        "support them (set confident=true when the answer states them; confident=false "
        "for a supported-but-approximate range). Only leave NULL if there is genuinely "
        "no basis. Do not fabricate precise years with no support."
    )
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=2500,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_years"},
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
                    help="only forklifts that currently have no year_start")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    if not settings.web_lookup_enabled:
        raise SystemExit("ANTHROPIC_API_KEY and TAVILY_API_KEY must be set in .env.")

    db = SessionLocal()
    q = db.query(Forklift)
    if args.oem:
        q = q.filter(Forklift.manufacturer.ilike(args.oem))
    if args.missing_only:
        q = q.filter(Forklift.year_start.is_(None))

    groups: dict[tuple[str, str], list[Forklift]] = defaultdict(list)
    for fk in q.all():
        groups[(fk.manufacturer, fk.series or "")].append(fk)

    if args.list:
        for (oem, series), fks in sorted(groups.items()):
            print(f"{len(fks):3}  {oem}  [{series or 'no series'}]")
        print(f"\n{len(groups)} groups.")
        db.close()
        return

    print(f"Processing {len(groups)} family group(s)"
          + (f" for '{args.oem}'" if args.oem else "") + " ...")
    new_file = not os.path.exists(REPORT)
    with open(REPORT, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["oem", "model", "year_start", "year_end", "source_url", "confident"])
        set_count, low = 0, 0
        for i, ((oem, series), fks) in enumerate(sorted(groups.items()), 1):
            models = [f.model for f in fks]
            index = {m.strip().lower(): f for m, f in zip(models, fks)}
            try:
                results, source = resolve_group(oem, models)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}/{len(groups)}] {oem} [{series}]: error {e}")
                continue
            got = 0
            for r in results:
                fk = index.get((r.get("model") or "").strip().lower())
                if not fk:
                    continue
                ys, ye = r.get("year_start"), r.get("year_end")
                if ys is None and ye is None:
                    writer.writerow([oem, fk.model, "", "", source, r.get("confident")])
                    continue
                fk.year_start, fk.year_end = ys, ye
                got += 1
                set_count += 1
                if not r.get("confident"):
                    low += 1
                writer.writerow([oem, fk.model, ys, ye, source, bool(r.get("confident"))])
            db.commit()
            fh.flush()
            print(f"  [{i}/{len(groups)}] {oem} [{series}] -> years on {got}/{len(fks)}")
            if args.sleep and i < len(groups):
                time.sleep(args.sleep)

    db.close()
    print(f"Done. Set years on {set_count} ({low} low-confidence). Review: {REPORT}")


if __name__ == "__main__":
    main()
