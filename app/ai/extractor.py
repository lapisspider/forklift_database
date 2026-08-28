"""Use Claude to turn raw spec-sheet text into structured ForkliftSpecs."""
from __future__ import annotations

import json

from anthropic import Anthropic

from ..config import settings
from ..schemas import ForkliftSpecs
from . import tavily_client

_SYSTEM = """You extract forklift specifications from raw web/PDF text.
Return ONLY the fields you are confident about; leave anything uncertain as null.
Rated load capacity must be in kilograms (kg) — convert from pounds if needed.
fuel_type must be one of: electric, LPG, diesel, gasoline.
chassis: the shared-chassis grouping/frame the model uses, if the source names one
(e.g. a frame/chassis class); leave null if not stated.

ALWAYS determine the `series`: the manufacturer's OFFICIAL published
series/family NAME that THIS specific model belongs to, as named in the spec
document or marketing (e.g. Hyster "Fortis", "XT", the "ESC AD" stacker series;
Toyota "8-Series"; Crown "FC 5700 series"). This is the manufacturer's own name
for the product line -- NOT a raw model-number range. Prefer the exact series
name printed in the source. Only leave series null if the source does not name
one.

Also determine the model's PRODUCTION YEARS (best effort): year_start = first
production year, year_end = last production year (null if still in production).
Only provide years you are reasonably confident about -- if you do not know,
leave BOTH null. NEVER guess a year."""


def _client() -> Anthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=settings.anthropic_api_key)


def extract_specs(raw_text: str, hint: str = "") -> ForkliftSpecs:
    """Extract structured specs from spec-sheet text.

    `hint` is the user's original query (e.g. "Toyota 8FGCU25"), used to
    disambiguate when a page lists several models.
    """
    client = _client()
    # Keep the payload bounded — spec sheets are small, web pages can be huge.
    text = raw_text[:60_000]

    tool = {
        "name": "record_specs",
        "description": "Record the extracted forklift specifications.",
        "input_schema": ForkliftSpecs.model_json_schema(),
    }
    user = (
        f"User asked about: {hint}\n\n" if hint else ""
    ) + f"Spec-sheet text:\n\n{text}"

    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=1024,
        system=_SYSTEM,
        tools=[tool],
        tool_choice={"type": "tool", "name": "record_specs"},
        messages=[{"role": "user", "content": user}],
    )
    for block in resp.content:
        if block.type == "tool_use" and block.name == "record_specs":
            return ForkliftSpecs.model_validate(block.input)
    # Fallback: nothing usable came back.
    return ForkliftSpecs()


def find_production_years(manufacturer: str, model: str) -> tuple[int | None, int | None]:
    """Best-effort production-year span via a dedicated web search.

    Spec sheets rarely print production years, so the main extractor almost
    always returns null. This runs a targeted Tavily AI-answer search (same
    technique as backfill_years.py) and asks Claude to read it. Returns
    (year_start, year_end); either/both may be None. Never raises.
    """
    name = f"{manufacturer or ''} {model or ''}".strip()
    if not name:
        return None, None
    query = (f"What years were the {name} forklift manufactured? "
             f"production start year and end year")
    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001 — year lookup is best-effort, never fatal
        return None, None
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:15_000]
    if not content.strip():
        return None, None

    tool = {
        "name": "record_years",
        "description": "Record the model's production year span.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year_start": {"type": ["integer", "null"],
                               "description": "first year made"},
                "year_end": {"type": ["integer", "null"],
                             "description": "last year made; null if still in production"},
            },
        },
    }
    prompt = (
        f"Model: {name}\n\nWeb search results:\n{content}\n\n"
        "Give this model's PRODUCTION year span: year_start (first year made) and "
        "year_end (last year made; null if still in production). The 'AI SEARCH ANSWER' "
        "often states these directly — use it, the content, and your knowledge. Only "
        "leave a value NULL if there is genuinely no basis. Do not fabricate precise "
        "years with no support."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=300,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_years"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_years":
                return block.input.get("year_start"), block.input.get("year_end")
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


def find_series(manufacturer: str, model: str) -> str | None:
    """Best-effort official series/family name via a dedicated web search.

    Like find_production_years: many spec pages don't name the series in the
    text the extractor sees, so this runs a targeted Tavily AI-answer search.
    Returns the manufacturer's published series name, or None. Never raises.
    """
    name = f"{manufacturer or ''} {model or ''}".strip()
    if not name:
        return None
    query = (f"What product series or family does the {name} forklift belong to? "
             f"manufacturer's official series name")
    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001
        return None
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:15_000]
    if not content.strip():
        return None

    tool = {
        "name": "record_series",
        "description": "Record the model's official series/family name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "series": {
                    "type": ["string", "null"],
                    "description": "The manufacturer's OFFICIAL published series/family "
                                   "name this model belongs to (e.g. Hyster 'Fortis', "
                                   "Toyota '8-Series'). NOT a raw model-number range. "
                                   "null if the sources don't name one.",
                },
            },
        },
    }
    prompt = (
        f"Model: {name}\n\nWeb search results:\n{content}\n\n"
        "What is this model's OFFICIAL series/family name as published by the "
        "manufacturer? Use the 'AI SEARCH ANSWER', the content, and your knowledge. "
        "Return the manufacturer's own product-line name — NOT a raw model-number "
        "range. Leave null only if there is genuinely no basis."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=200,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_series"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_series":
                series = (block.input.get("series") or "").strip()
                return series or None
    except Exception:  # noqa: BLE001
        return None
    return None
