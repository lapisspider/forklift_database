"""Use Claude to turn raw spec-sheet text into structured ForkliftSpecs."""
from __future__ import annotations

import json
import re
import time

from anthropic import Anthropic

from ..config import settings
from ..schemas import ForkliftSpecs, MarketEquivalent, SerialWebGuess, clean_series
from . import tavily_client

_SYSTEM = """You extract forklift specifications from raw web/PDF text.
Return ONLY the fields you are confident about; leave anything uncertain as null.
Rated load capacity must be in kilograms (kg). If the source prints a kg figure,
USE IT VERBATIM — do NOT recompute it from the pounds figure. An OEM's lb and kg
ratings are two separately published numbers, not conversions of each other, so
converting 6000 lb gives 2722 kg where the OEM actually publishes 3000 kg. Convert
from pounds ONLY when no kg figure appears anywhere in the source.
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
document or marketing (e.g. Hyster "Fortis", "XT", the "ESC AD" stacker range).
This is the manufacturer's own name for the product line -- NOT a raw
model-number range. Prefer the exact series name printed in the source. Only
leave series null if the source does not name one.
NEVER include the word "Series" (or "series") in the value, and never a
generation suffix built from it: Toyota's 8-Series is "8", Hyundai's 9 Series is
"9", Crown's "FC 5700 series" is "FC 5700". Give the bare name only.

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
                                   "name this model belongs to (e.g. Hyster 'Fortis'). "
                                   "NOT a raw model-number range, and NEVER containing "
                                   "the word 'Series' -- Toyota's 8-Series is '8'. "
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
                return clean_series(block.input.get("series"))
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
                        "description": "Rated load capacity in kilograms, as the source prints it. Convert from lb only if no kg figure is published."},
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


# OEMs whose model code states capacity in metric units (100 kg), so the code
# itself is an independent check on the extracted figure. Deliberately excludes
# Toyota, Hyster, Yale, Clark, Crown and the other NA brands, whose codes state
# POUNDS (a Hyster H50FT is 5000 lb / 2268 kg, not 5000 kg).
_METRIC_CODE_OEMS = {
    "Linde", "Komatsu", "Hyundai", "Doosan", "Daewoo", "Heli", "Hangcha", "BYD",
    "EP", "Jungheinrich", "Still", "Mitsubishi", "Nissan", "TCM", "Lonking", "Liugong",
}
_FIRST_DIGITS_RE = re.compile("[0-9]{2,3}")


def reconcile_capacity(manufacturer: str | None, model: str | None,
                       capacity_kg: float | None) -> tuple[float | None, str | None]:
    """Repair a capacity that was computed from the pounds rating.

    An lb rating converted to kg lands just under the figure the OEM publishes --
    6000 lb becomes 2722 where Hyundai publishes 3000 for the same 30D-9. For the
    OEMs whose model code states capacity in 100 kg, the code settles it.

    The 12% band is the real guard: it fires only where the code and the extracted
    value already agree, so a rounding artifact is corrected while a code whose
    digits are not a capacity at all is left alone (Jungheinrich EFG 320 is a
    2000 kg truck, so the "320" is 32000 kg away and never applied).

    Returns (capacity, note) -- note is None when nothing was changed.
    """
    if capacity_kg is None or (manufacturer or "") not in _METRIC_CODE_OEMS:
        return capacity_kg, None
    m = _FIRST_DIGITS_RE.search(model or "")
    if not m:
        return capacity_kg, None
    coded = int(m.group(0)) * 100
    if not 1000 <= coded <= 46000:
        return capacity_kg, None
    if coded == capacity_kg or abs(coded - capacity_kg) / coded > 0.12:
        return capacity_kg, None
    return float(coded), (
        f"Capacity corrected from {capacity_kg:g} kg to {coded} kg: the extracted figure was "
        f"converted from the pounds rating, and {manufacturer} states capacity in the model code."
    )


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


_EQUIV_SYSTEM = """You answer one narrow question: what a given forklift model is
called in another market, and whether it is sold there under its own name.

The same truck is routinely badged differently by region -- a model sold in Europe
or Asia often reaches North America under a different designation, and sometimes is
not offered there at all.

Rules you must follow:
- Name a counterpart ONLY if the search content supports it. An invented model
  number is worse than no answer: say so in `reasoning` and return an empty list.
- A counterpart must be a designation USED IN THE TARGET MARKET. The asked
  model's own name, a variant of it, or the series range containing it is NOT a
  counterpart -- for a Yale ERC040VAN, "ERC040VA" is the same North American
  truck without its order-code suffix and "ERC030-040VA" is that truck's North
  American spec-sheet range. Neither is a European name. A cross-reference table
  from the model's HOME market is not evidence of a target-market name either.
  If that is all the search supports, return an empty list and say so.
- A counterpart must be in the SAME capacity class as the asked model. A 1.8 tonne
  truck's counterpart is not a 4.0 tonne truck. If the only thing you can find is a
  different size, do not list it -- explain that in `reasoning` instead.
- Capacity is written differently per market, and that is the strongest signal
  you have. North American codes carry hundreds of POUNDS (Yale ERC040VAN =
  4000 lb); European codes carry TONNES (Hyster J2.0XN = 2.0 t). A counterpart
  whose code carries the asked model's own number, in the asked model's own
  units, is almost certainly the same truck rather than its counterpart.
- `candidate_models` is model designations only ("GC25N5"), never prose.
- "Not sold there" and "sold there under another name" are different answers. Do
  not collapse them."""


_EQUIV_CACHE_TTL = 600  # 10 minutes, matching web_identify's own cache
_equiv_cache: dict[str, tuple[float, MarketEquivalent]] = {}

# Markets whose model codes state capacity in tonnes rather than hundreds of
# pounds. Matched loosely against the target-market phrase.
_METRIC_MARKET_WORDS = ("europe", "european", "emea", "uk", "britain", "germany",
                        "france", "asia", "australia", "international")


def _capacity_bridge(capacity_kg: float | None, target_market: str) -> str | None:
    """The arithmetic that turns an American designation into a European one.

    A North American code states capacity in hundreds of POUNDS -- a Yale
    ERC040VAN is 4000 lb. A European code states it in TONNES -- a Hyster
    J2.0XN is 2.0 t. The same truck therefore carries a different number in
    each market, so knowing the capacity tells us what number the counterpart's
    code should carry. That is a local calculation, and it is worth far more
    than another search: it gives the model something to recognise a real
    counterpart BY, instead of reaching for whatever string looked closest.
    """
    if not capacity_kg:
        return None
    tonnes = capacity_kg / 1000.0
    hundred_lb = round(capacity_kg * 2.20462 / 100.0)
    metric = any(w in (target_market or "").lower() for w in _METRIC_MARKET_WORDS)
    if metric:
        return (f"The asked model is {capacity_kg:,.0f} kg. North American codes state "
                f"capacity in hundreds of pounds ({hundred_lb:03d} = {hundred_lb * 100:,} lb); "
                f"codes in {target_market} state it in tonnes. So a counterpart in "
                f"{target_market} should carry roughly {tonnes:.1f} in its code (or "
                f"{tonnes * 10:.0f} where tenths are written without the point), NOT "
                f"{hundred_lb:03d}.")
    return (f"The asked model is {capacity_kg:,.0f} kg ({tonnes:.1f} tonnes). North American "
            f"codes state capacity in hundreds of pounds, so a counterpart in {target_market} "
            f"should carry roughly {hundred_lb:03d} in its code ({hundred_lb * 100:,} lb), NOT "
            f"{tonnes:.1f}.")


def _equiv_round(query: str) -> tuple[str, str | None]:
    """One Tavily answer-search flattened to (content, first_url). ("", None) on failure."""
    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001 -- best-effort, never fatal
        return "", None
    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    return content[:9_000], (results[0].get("url") if results else None)


def find_market_equivalent(manufacturer: str, model: str,
                           target_market: str = "the United States",
                           specs_hint: str | None = None,
                           capacity_kg: float | None = None) -> MarketEquivalent:
    """What is this model called in `target_market`, and is it sold there?

    Same shape as find_series/find_truck_class: targeted Tavily AI-answer
    searches plus Claude, returning a claim off the web -- NOT a verified fact.
    crossref.py checks any named candidate against the catalog before the
    answer presents it. Never raises.

    TWO searches, not one. A single question asking "is it sold there, what is
    it called there, and where IS it sold" reads as a question about the BRAND
    rather than the truck, and comes back with corporate history -- the first
    version of this returned the Mitsubishi Caterpillar joint-venture Wikipedia
    page for a DP18NTD. The second round asks only for the counterpart
    designation, and only runs when the first never found the model at all.

    `specs_hint` carries what is already known about the truck (capacity, fuel,
    class) so the model can reject a counterpart of the wrong size itself,
    instead of leaving that entirely to the catalog check downstream.
    """
    name = f"{manufacturer or ''} {model or ''}".strip()
    if not name:
        return MarketEquivalent(reasoning="No model given.")

    # Up to two Tavily searches per call, so a repeated question (the same one
    # retyped, or a page reload) must not re-spend them.
    key = f"equiv|{name.lower()}|{target_market.lower()}"
    hit = _equiv_cache.get(key)
    if hit and (time.time() - hit[0]) < _EQUIV_CACHE_TTL:
        return hit[1]

    # Two searches, always, because they fail in opposite directions and each
    # one finds what the other misses.
    #
    # The model-led query ranks whatever repeats the model string most often,
    # which for a North American truck is North American parts and auction
    # sites -- that is how "ERC040VA", a string those pages sit beside, came
    # back as the European name of an ERC040VAN. But the same query is exactly
    # what surfaces an OEM interchange table, which is where the Cat DP18NTD's
    # counterpart was found.
    #
    # The chart query avoids the model string and anchors on capacity instead,
    # which reaches the target market's own range -- but only when that range
    # is published as a chart, and it says nothing about a specific truck.
    #
    # Running one or the other lost a case each time, so run both and let the
    # model read them together. Two searches is what this already cost at
    # worst, and the result is cached for ten minutes.
    tonnes = f"{capacity_kg / 1000:.1f} tonne " if capacity_kg else ""
    chart, chart_url = _equiv_round(
        f'{manufacturer} forklift {target_market} equivalent model number capacity '
        f'matrix cross reference chart {tonnes}'.strip()
    )
    named, named_url = _equiv_round(
        f'{manufacturer} "{model}" forklift {target_market} equivalent model number '
        f'cross reference what is it called'
    )
    content = "\n\n".join(p for p in (chart, named) if p)
    # Prefer the source that actually mentioned the truck.
    source_url = (named_url if named and model.lower() in named.lower()
                  else chart_url or named_url)
    content = content[:12_000]

    tool = {
        "name": "record_equivalent",
        "description": "Record the market counterpart for a forklift model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sold_in_target_market": {
                    "type": ["boolean", "null"],
                    "description": ("True if the ASKED model is itself offered in the target "
                                    "market under that same name. Null if the content does "
                                    "not say."),
                },
                "home_markets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Regions the asked model IS sold in, e.g. ['Europe', 'Asia'].",
                },
                "candidate_models": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Model designations sold in the target market that are the "
                                    "same truck or its direct replacement, same capacity class. "
                                    "Empty if the content does not support one."),
                },
                "reasoning": {
                    "type": "string",
                    "description": ("One or two sentences a forklift dealer would accept, saying "
                                    "what the evidence was. If no counterpart could be named, say "
                                    "why."),
                },
            },
            "required": ["reasoning"],
        },
    }
    prompt = (
        f"Model: {name}\nTarget market: {target_market}\n"
        + f"Do NOT return '{model}', a variant of it, or the series range "
          f"containing it -- those are the asked model itself, not a counterpart.\n"
        + ((_capacity_bridge(capacity_kg, target_market) or "") + "\n")
        + (f"Known specs of the asked model: {specs_hint}\n" if specs_hint else "")
        + f"\nWeb search results:\n{content or '(no content retrieved)'}\n\n"
        f"What is the {target_market} counterpart of the {name}, and is the {name} itself "
        f"sold there?"
    )
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=400,
            system=_EQUIV_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_equivalent"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_equivalent":
                data = dict(block.input)
                data["source_url"] = source_url
                found = MarketEquivalent.model_validate(data)
                _equiv_cache[key] = (time.time(), found)
                return found
    except Exception as e:  # noqa: BLE001
        # Not cached: a transport failure should be retried, not remembered.
        return MarketEquivalent(reasoning=f"Lookup failed: {e}", source_url=source_url)
    empty = MarketEquivalent(reasoning="No counterpart returned.", source_url=source_url)
    _equiv_cache[key] = (time.time(), empty)
    return empty
