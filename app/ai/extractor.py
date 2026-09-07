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
fuel_type must be EXACTLY one of these canonical values (match the casing):
  "Electric", "Diesel", "LPG", "Gasoline",
  "Gasoline/LPG"  (gasoline/LP dual-fuel — runs on either gasoline or LP gas),
  "Diesel/LPG"    (offered in either diesel or LP gas).
Map synonyms: propane/LP/LP gas/liquefied petroleum -> "LPG"; battery -> "Electric".
A dual-fuel gasoline+LP truck is "Gasoline/LPG"; a diesel+LP truck is "Diesel/LPG".
Leave fuel_type null if the source doesn't state it — do not guess.
chassis: the shared-chassis grouping/frame the model uses, if the source names one
(e.g. a frame/chassis class); leave null if not stated.

ALWAYS determine `truck_class`: the OSHA powered-industrial-truck class, output as
"Class I".."Class VII":
  Class I   = Electric Motor Rider trucks (electric counterbalance sit-down/stand-up).
  Class II  = Electric Motor Narrow-Aisle trucks (reach trucks, order pickers, turret).
  Class III = Electric Motor Hand/Hand-Rider trucks (walkie pallet jacks, walkie stackers).
  Class IV  = Internal-combustion engine trucks with CUSHION (solid) tires (indoor).
  Class V   = Internal-combustion engine trucks with PNEUMATIC tires (outdoor).
  Class VI  = Electric or IC engine TRACTORS (tow tractors / tuggers).
  Class VII = Rough-Terrain forklift trucks.
Decide from the model's type + fuel + tire: electric counterbalance -> I; electric
reach/order-picker/narrow-aisle -> II; electric walkie/pallet/stacker -> III; IC cushion
-> IV; IC pneumatic -> V; tow tractor -> VI; rough terrain -> VII. Give your best-effort
class even if tire type is not explicitly stated (infer from the model line).
IMPORTANT: an ELECTRIC COUNTERBALANCE rider is Class I whether its tires are cushion OR
pneumatic — cushion tires do NOT make it Class II. Class II is ONLY narrow-aisle
warehouse trucks (reach, order picker, turret, swing-mast); a counterbalance truck is
NEVER Class II. Tire type (cushion vs pneumatic) only distinguishes Class IV from V.

ALWAYS determine the `series`: the manufacturer's OFFICIAL published
series/family NAME that THIS specific model belongs to, as named in the spec
document or marketing (e.g. Hyster "Fortis", "XT", the "ESC AD" stacker series;
Toyota "8-Series"; Crown "FC 5700 series"). This is the manufacturer's own name
for the product line -- NOT a raw model-number range. Prefer the exact series
name printed in the source. Only leave series null if the source does not name
one.

Also determine the model's PRODUCTION YEARS: year_start = first production year,
year_end = last production year (null if still in production). Prefer years stated in
the source or that you know; if they are not stated, give a best-effort EDUCATED
ESTIMATE from the model generation rather than leaving them blank (the user reviews
and confirms every lookup before it is saved)."""


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


_CLASS_RULES = (
    "OSHA powered-industrial-truck classes: "
    "Class I = electric motor rider (electric counterbalance); "
    "Class II = electric narrow-aisle (reach truck, order picker, turret); "
    "Class III = electric hand/walkie (walkie pallet jack, walkie stacker); "
    "Class IV = internal-combustion, CUSHION (solid) tires; "
    "Class V = internal-combustion, PNEUMATIC tires; "
    "Class VI = electric or IC tractor (tow tractor); "
    "Class VII = rough-terrain forklift. "
    "NOTE: an electric COUNTERBALANCE rider is Class I whether cushion OR pneumatic tire; "
    "Class II is ONLY narrow-aisle (reach/order-picker/turret), never a counterbalance."
)


def find_truck_class(manufacturer: str, model: str) -> str | None:
    """Best-effort OSHA class ("Class I".."Class VII") via a dedicated web search.

    Same shape as find_series/find_production_years: targeted Tavily AI-answer search
    plus Claude, mapping the truck's type/fuel/tire to an OSHA class. Never raises.
    """
    name = f"{manufacturer or ''} {model or ''}".strip()
    if not name:
        return None
    query = (f"Is the {name} forklift electric or internal combustion, what tire type "
             f"(cushion or pneumatic), and is it a rider, reach, walkie, tow tractor, "
             f"or rough-terrain truck?")
    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001
        return None
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:12_000]

    tool = {
        "name": "record_class",
        "description": "Record the OSHA powered-industrial-truck class.",
        "input_schema": {
            "type": "object",
            "properties": {
                "truck_class": {
                    "type": ["string", "null"],
                    "enum": ["Class I", "Class II", "Class III", "Class IV",
                             "Class V", "Class VI", "Class VII", None],
                    "description": "OSHA class as 'Class I'..'Class VII'.",
                },
            },
        },
    }
    prompt = (
        f"Model: {name}\n\n{_CLASS_RULES}\n\nWeb search results:\n"
        f"{content or '(no content retrieved)'}\n\n"
        "Give the single best-fit OSHA class as 'Class I'..'Class VII'. Use the search "
        "content and your knowledge; infer tire type from the model line if not stated. "
        "Only return null if there is genuinely no basis."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=100,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_class"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_class":
                tc = (block.input.get("truck_class") or "").strip()
                return tc or None
    except Exception:  # noqa: BLE001
        return None
    return None
