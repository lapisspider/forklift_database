"""Use Claude to turn raw spec-sheet text into structured ForkliftSpecs."""
from __future__ import annotations

import json

from anthropic import Anthropic

from ..config import settings
from ..schemas import ForkliftSpecs, SerialWebGuess
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


_PUBLISHED_YEARS_ONLY = (
    "\n\nOVERRIDE for production years: set year_start/year_end ONLY if the text "
    "explicitly states them. Otherwise leave both null -- never estimate them here; "
    "estimation is handled separately and labelled."
)


def extract_specs(raw_text: str, hint: str = "", published_years_only: bool = False) -> ForkliftSpecs:
    """Extract structured specs from spec-sheet text.

    `hint` is the user's original query (e.g. "Toyota 8FGCU25"), used to
    disambiguate when a page lists several models. `published_years_only`
    stops the model guessing years, so any year returned is a stated one.
    """
    client = _client()
    # Keep the payload bounded — spec sheets are small, web pages can be huge.
    text = raw_text[:60_000]

    schema = ForkliftSpecs.model_json_schema()
    system = _SYSTEM
    if published_years_only:
        system += _PUBLISHED_YEARS_ONLY
        for f in ("year_start", "year_end"):
            schema["properties"][f]["description"] = "Production year, only if explicitly stated in the text; else null."
    tool = {
        "name": "record_specs",
        "description": "Record the extracted forklift specifications.",
        "input_schema": schema,
    }
    user = (
        f"User asked about: {hint}\n\n" if hint else ""
    ) + f"Spec-sheet text:\n\n{text}"

    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=1024,
        system=system,
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
    always returns null. This runs a targeted Tavily AI-answer search and
    asks Claude to read it. Returns
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


def find_core_specs(manufacturer: str, model: str, missing: list[str]) -> dict:
    """One targeted web re-query for any of capacity_kg / fuel_type / chassis the
    first extraction missed. Returns only the fields it could source. Never raises."""
    name = f"{manufacturer or ''} {model or ''}".strip()
    wanted = [f for f in missing if f in ("capacity_kg", "fuel_type", "chassis")]
    if not name or not wanted:
        return {}
    query = (f"{name} forklift rated load capacity lb, fuel type (electric, LPG, gasoline, diesel), "
             f"and chassis or frame")
    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001
        return {}
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:15_000]
    if not content.strip():
        return {}

    props = {
        "capacity_kg": {"type": ["number", "null"],
                        "description": "Rated load capacity in kilograms (convert from lb)."},
        "fuel_type": {"type": ["string", "null"],
                      "description": "Exactly one of: Electric, Diesel, LPG, Gasoline, "
                                     "Gasoline/LPG, Diesel/LPG."},
        "chassis": {"type": ["string", "null"],
                    "description": "Shared chassis/frame grouping, only if a source names one."},
    }
    tool = {
        "name": "record_core",
        "description": "Record the missing core specs.",
        "input_schema": {"type": "object", "properties": {k: props[k] for k in wanted}},
    }
    prompt = (
        f"Model: {name}\n\nWeb search results:\n{content}\n\n"
        f"Fill in these fields for this exact model: {', '.join(wanted)}. Use only what the "
        "results support (or well-established fact for capacity/fuel). Leave a field null "
        "rather than guess; never invent a chassis name."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=300,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_core"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_core":
                out = {k: v for k, v in block.input.items() if k in wanted and v not in (None, "")}
                return out
    except Exception:  # noqa: BLE001
        return {}
    return {}


def estimate_production_years(manufacturer: str, model: str, series: str | None,
                              evidence: str) -> tuple[int | None, int | None]:
    """Educated production-year estimate when no source states them.
    year_end None = probably still in production."""
    name = f"{manufacturer or ''} {model or ''}".strip()
    if not name:
        return None, None
    tool = {
        "name": "record_estimate",
        "description": "Record the estimated production span.",
        "input_schema": {
            "type": "object",
            "properties": {
                "year_start": {"type": ["integer", "null"]},
                "year_end": {"type": ["integer", "null"],
                             "description": "null if the model is probably still in production"},
            },
        },
    }
    prompt = (
        f"Truck: {name}\n"
        + (f"Series: {series}\n" if series else "")
        + f"\nSource text (no production years are stated in it):\n{(evidence or '')[:6000]}\n\n"
        "No published production years could be found. Make your best EDUCATED ESTIMATE of "
        "year_start and year_end from the series generation, engine/emissions tier, catalogue "
        "or brochure context, and your knowledge of this product line. Prefer a plausible "
        "span over null; null year_end if probably still sold. Only return null years if the "
        "truck cannot be identified at all."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=200,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_estimate"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_estimate":
                return block.input.get("year_start"), block.input.get("year_end")
    except Exception:  # noqa: BLE001
        return None, None
    return None, None


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


_SERIAL_ID_SYSTEM = """You identify what forklift a serial number belongs to,
from web search excerpts. State ONLY what the excerpts actually support --
never guess a manufacturer, model, or year that isn't backed by the text.
NEVER infer a year from the digits of the serial/sequence number itself
(e.g. do not read "00116" as "2016" or similar) -- a year is only valid if
the excerpts state it in words or as a documented production span.
If the excerpts don't support a field, leave it null. Set confidence "low"
if the match is a guess from partial context, "medium" if plausible but not
explicit, "high" only if a source directly names this exact serial or a
clearly matching model/serial pattern."""


def identify_from_serial(raw: str, manufacturer: str | None, content: str,
                          source_url: str | None) -> SerialWebGuess:
    """Best-effort manufacturer/model/year guess for a serial that didn't
    decode offline. Always unverified -- the caller renders it as such and
    hands off to the normal review-then-save form; this never writes anything."""
    if not content.strip():
        return SerialWebGuess(confidence="low", reasoning="No web content retrieved.",
                               source_url=source_url)

    tool = {
        "name": "record_guess",
        "description": "Record the best-effort identification of a forklift from its serial number.",
        "input_schema": {
            "type": "object",
            "properties": {
                "manufacturer": {"type": ["string", "null"]},
                "model_family": {"type": ["string", "null"],
                                  "description": "series/family name, if the excerpts name one"},
                "model": {"type": ["string", "null"]},
                "year_start": {"type": ["integer", "null"]},
                "year_end": {"type": ["integer", "null"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "reasoning": {"type": "string",
                              "description": "1-2 sentences: what in the excerpts supports this"},
            },
            "required": ["confidence", "reasoning"],
        },
    }
    prompt = (
        f"Serial number: {raw}\n"
        + (f"User-supplied OEM hint: {manufacturer}\n" if manufacturer else "")
        + f"\nWeb search results:\n{content}\n\n"
        "Identify the manufacturer, model family, and specific model this serial belongs to, "
        "and its production year span, using ONLY what the excerpts support."
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=400,
            system=_SERIAL_ID_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_guess"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_guess":
                data = dict(block.input)
                data["source_url"] = source_url
                return SerialWebGuess.model_validate(data)
    except Exception as e:  # noqa: BLE001
        return SerialWebGuess(confidence="low", reasoning=f"Lookup failed: {e}", source_url=source_url)
    return SerialWebGuess(confidence="low", reasoning="No identification returned.", source_url=source_url)
