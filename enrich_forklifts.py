"""Populate individual forklift models (with spec sheets) from each kit's
"fits" text, using web search (Tavily) + Claude, and link them to the kit.

This is the heavy, credit-spending, review-worthy step. Run it in small
batches — per OEM and/or with --limit — and review enrich_report.csv before
continuing. It is resumable: forklifts already created (matched by OEM+model)
are reused, not duplicated.

Usage:
  python enrich_forklifts.py --oem Toyota            # one OEM
  python enrich_forklifts.py --oem Toyota --limit 3  # cap kits processed
  python enrich_forklifts.py --limit 1               # smoke test, any OEM
  python enrich_forklifts.py --list                  # list OEMs + kit counts

Requires ANTHROPIC_API_KEY and TAVILY_API_KEY in .env.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time

from anthropic import Anthropic

from app.ai import tavily_client
from app.config import settings
from app.database import SessionLocal
from app.main import next_company_serial
from app.models import Forklift, Kit

REPORT = "enrich_report.csv"


def normalize_model(name: str) -> str:
    """Canonical form for dedupe: uppercase, strip spaces around hyphens."""
    s = re.sub(r"\s*-\s*", "-", name.upper())
    return re.sub(r"\s+", " ", s).strip()


def is_valid_model(name: str) -> bool:
    """Reject junk (single chars, punctuation, fragments)."""
    if len(name) < 2 or len(name) > 60:
        return False
    if not re.search(r"[A-Za-z]", name):   # must contain a letter
        return False
    if not re.search(r"[A-Za-z0-9]{2,}", name):  # at least 2 alnum in a row
        return False
    if re.search(r"\bseries\b", name, re.IGNORECASE):  # the series name, not a model
        return False
    return True


def parse_notes(notes: str) -> tuple[str, str]:
    """Return (oem, fits_text) from a kit's notes blob."""
    oem, fits = "Unknown", ""
    for line in (notes or "").splitlines():
        if line.startswith("OEM:"):
            oem = line[4:].strip()
        elif line.startswith("Fits:"):
            fits = line[5:].strip()
    return oem, fits


def enumerate_models(oem: str, fits_text: str, web_content: str) -> list[dict]:
    """Ask Claude to list the individual models covered by the fits text."""
    client = Anthropic(api_key=settings.anthropic_api_key)
    tool = {
        "name": "record_models",
        "description": "Record the individual forklift models.",
        "input_schema": {
            "type": "object",
            "properties": {
                "models": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "model": {"type": "string", "description": "exact model designation"},
                            "series": {"type": ["string", "null"], "description":
                                       "manufacturer family/range this model belongs to, "
                                       "verbatim (e.g. 'E80-120XN', 'FC 5700 series')"},
                            "year_start": {"type": ["integer", "null"], "description":
                                           "first production year; null if unknown (never guess)"},
                            "year_end": {"type": ["integer", "null"], "description":
                                         "last production year; null if still made or unknown"},
                            "capacity_kg": {"type": ["number", "null"]},
                            "fuel_type": {"type": ["string", "null"]},
                        },
                        "required": ["model"],
                    },
                }
            },
            "required": ["models"],
        },
    }
    prompt = (
        f"OEM: {oem}\n"
        f"This kit fits: {fits_text}\n\n"
        f"Reference web content:\n{web_content[:20000]}\n\n"
        "List the distinct INDIVIDUAL forklift model designations this kit fits.\n"
        "Rules:\n"
        "- Use the model names as the manufacturer writes them (e.g. 'H50FT').\n"
        "- Do NOT invent capacity sub-variants, tonnage suffixes, or padding you\n"
        "  cannot see in the fits text or content. Prefer fewer, correct models.\n"
        "- If the fits text is a plain range like 'H45-70FT', return only the\n"
        "  clearly-real endpoint/step models, not every number in between.\n"
        "- Never return single characters or fragments. Cap at 15 models.\n"
        "- For each model, also give its `series`: the manufacturer's OFFICIAL\n"
        "  published series/family NAME for that model, as found in the web content\n"
        "  (e.g. Hyster 'Fortis', 'ESC AD' series; Toyota '8-Series'). This is the\n"
        "  manufacturer's product-line name, NOT a raw model-number range. Leave\n"
        "  null if the content doesn't name one."
    )
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=1500,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_models"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            models = block.input.get("models", [])
            # Claude sometimes returns the array as a JSON string.
            if isinstance(models, str):
                try:
                    models = json.loads(models)
                except (ValueError, TypeError):
                    models = []
            return models if isinstance(models, list) else []
    return []


def process_kit(db, kit: Kit, writer) -> int:
    oem, fits_text = parse_notes(kit.notes or "")
    if not fits_text:
        writer.writerow([kit.sku, oem, "", "", "", "no fits text"])
        return 0

    search = tavily_client.search_spec_sheet(f"{oem} {fits_text}")
    results = search.get("results", [])
    top_url = results[0].get("url") if results else ""
    pdf_url = tavily_client.best_pdf_url(search) or ""
    content = "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )

    try:
        models = enumerate_models(oem, fits_text, content)
    except Exception as e:  # noqa: BLE001
        writer.writerow([kit.sku, oem, fits_text[:60], "", "", f"enumerate error: {e}"])
        return 0

    created = 0
    seen_norm: set[str] = set()
    for m in models:
        # Claude may return a plain string or an object — handle both.
        if isinstance(m, str):
            m = {"model": m}
        elif not isinstance(m, dict):
            continue
        name = (m.get("model") or "").strip()
        if not is_valid_model(name):
            writer.writerow([kit.sku, oem, name, "", "", "rejected: invalid model name"])
            continue
        norm = normalize_model(name)
        if norm in seen_norm:
            continue  # formatting-duplicate within this kit's batch
        seen_norm.add(norm)
        # Dedupe against existing rows by normalized name.
        existing = next(
            (f for f in db.query(Forklift).filter(Forklift.manufacturer == oem).all()
             if normalize_model(f.model) == norm),
            None,
        )
        if existing:
            fk = existing
        else:
            fk = Forklift(
                manufacturer=oem,
                model=name,
                series=(m.get("series") or "").strip() or None,
                year_start=m.get("year_start"),
                year_end=m.get("year_end"),
                capacity_kg=m.get("capacity_kg"),
                fuel_type=m.get("fuel_type"),
                source_url=top_url or None,
                pdf_url=pdf_url or None,
                internal_serial=next_company_serial(db),
            )
            db.add(fk)
            db.flush()
            created += 1
        if fk not in kit.forklifts:
            kit.forklifts.append(fk)
        flag = "needs_review" if not (top_url or pdf_url) else "ok"
        writer.writerow([kit.sku, oem, name, top_url, pdf_url, flag])
    db.commit()
    return created


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oem", default=None, help="only kits whose OEM matches")
    ap.add_argument("--limit", type=int, default=None, help="max kits to process")
    ap.add_argument("--list", action="store_true", help="list OEMs and exit")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between kits")
    args = ap.parse_args()

    if not settings.web_lookup_enabled:
        sys.exit("ANTHROPIC_API_KEY and TAVILY_API_KEY must be set in .env.")

    db = SessionLocal()
    kits = db.query(Kit).order_by(Kit.sku).all()

    def kit_oem(k: Kit) -> str:
        return parse_notes(k.notes or "")[0]

    if args.list:
        counts: dict[str, int] = {}
        for k in kits:
            counts[kit_oem(k)] = counts.get(kit_oem(k), 0) + 1
        for oem, n in sorted(counts.items(), key=lambda x: -x[1]):
            print(f"{n:3}  {oem}")
        db.close()
        return

    todo = [k for k in kits if not args.oem or kit_oem(k).lower() == args.oem.lower()]
    if args.limit:
        todo = todo[: args.limit]

    print(f"Processing {len(todo)} kit(s)"
          + (f" for OEM '{args.oem}'" if args.oem else "") + " ...")
    new_report = not __import__("os").path.exists(REPORT)
    with open(REPORT, "a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_report:
            writer.writerow(["kit_sku", "oem", "model", "source_url", "pdf_url", "status"])
        total_created = 0
        for i, kit in enumerate(todo, 1):
            created = process_kit(db, kit, writer)
            total_created += created
            print(f"  [{i}/{len(todo)}] {kit.sku}: +{created} models")
            fh.flush()
            if args.sleep and i < len(todo):
                time.sleep(args.sleep)
    db.close()
    print(f"Done. Created {total_created} forklift model(s). Report: {REPORT}")


if __name__ == "__main__":
    main()
