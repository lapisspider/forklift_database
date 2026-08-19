"""Normalize the `series` values into consistent, canonical names.

No web calls. Collects the distinct (OEM, series) values currently in the DB —
with a few sample models for context — and asks Claude once to map each to a
clean canonical series name: merge misspellings/variants (Fortis/Fortens ->
Fortis), collapse "X" vs "X Series" duplicates, trim verbose descriptor tails
("SP 3500 Series Lifting Forks" -> "SP 3500 Series"), and resolve "<UNKNOWN>"
from the model names when possible. Applies the mapping and writes
normalize_report.csv (before -> after).

Usage:  python normalize_series.py
"""
from __future__ import annotations

import csv
from collections import defaultdict

from anthropic import Anthropic

from app.config import settings
from app.database import SessionLocal
from app.models import Forklift

REPORT = "normalize_report.csv"


def main() -> None:
    if not settings.ai_enabled:
        raise SystemExit("ANTHROPIC_API_KEY must be set in .env.")

    db = SessionLocal()
    # (oem, series) -> sample models
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for f in db.query(Forklift).all():
        if f.series:
            g = groups[(f.manufacturer, f.series)]
            if len(g) < 4:
                g.append(f.model)

    listing = "\n".join(
        f"- OEM={oem!r} series={series!r} models={models}"
        for (oem, series), models in sorted(groups.items())
    )

    tool = {
        "name": "record_canonical",
        "description": "Map each (oem, series) to a clean canonical series name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "oem": {"type": "string"},
                            "original": {"type": "string"},
                            "canonical": {"type": "string"},
                        },
                        "required": ["oem", "original", "canonical"],
                    },
                }
            },
            "required": ["mappings"],
        },
    }
    prompt = (
        "Below are the distinct forklift series values currently stored, per OEM, "
        "with sample models. Produce a clean CANONICAL series name for each.\n"
        "Rules:\n"
        "- Merge duplicates/misspellings/variants to ONE spelling "
        "(e.g. 'Fortis' and 'Fortens' -> 'Fortis'; 'X' and 'X Series' -> 'X Series').\n"
        "- Keep it concise: the manufacturer's series/family name plus 'Series' if "
        "natural. Trim verbose descriptor tails "
        "(e.g. 'SP 3500 Series Lifting Forks' -> 'SP 3500 Series'; "
        "'J series (Electric Cushion/Solid Tire...)' -> 'J Series').\n"
        "- Use consistent spacing/casing within an OEM.\n"
        "- For '<UNKNOWN>' or clearly-wrong values, infer the series from the model "
        "names; if impossible, return the best short family label you can.\n"
        "- Return EVERY (oem, original) pair, even if canonical == original.\n\n"
        f"{listing}"
    )

    client = Anthropic(api_key=settings.anthropic_api_key)
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=4000,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_canonical"},
        messages=[{"role": "user", "content": prompt}],
    )
    mapping: dict[tuple[str, str], str] = {}
    for block in resp.content:
        if block.type == "tool_use":
            for m in block.input.get("mappings", []):
                oem = (m.get("oem") or "").strip()
                orig = (m.get("original") or "").strip()
                canon = (m.get("canonical") or "").strip()
                if oem and orig and canon:
                    mapping[(oem, orig)] = canon

    changed = 0
    with open(REPORT, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["oem", "before", "after", "n_models"])
        for (oem, series), models in sorted(groups.items()):
            canon = mapping.get((oem, series))
            if not canon or canon == series:
                continue
            n = (
                db.query(Forklift)
                .filter(Forklift.manufacturer == oem, Forklift.series == series)
                .update({"series": canon})
            )
            changed += n
            writer.writerow([oem, series, canon, n])
        db.commit()
    db.close()
    print(f"Normalized. Updated {changed} forklift rows. Review: {REPORT}")


if __name__ == "__main__":
    main()
