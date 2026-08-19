"""Use Claude to turn raw spec-sheet text into structured ForkliftSpecs."""
from __future__ import annotations

import json

from anthropic import Anthropic

from ..config import settings
from ..schemas import ForkliftSpecs

_SYSTEM = """You extract forklift specifications from raw web/PDF text.
Return ONLY the fields you are confident about; leave anything uncertain as null.
Rated load capacity must be in kilograms (kg) — convert from pounds if needed.
fuel_type must be one of: electric, LPG, diesel, gasoline.

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
