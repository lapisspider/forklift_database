"""Orchestrates: check DB -> offline serial decode -> web search -> propose.

`mode` picks the strategy: "" (default, auto-detect -- see below), "model"
(skip decoding, model-lookup only), or "serial" (skip loose model text
search, decode-or-fail only). The two lookup tabs on the home page each post
their own `mode` and both REQUIRE a leading OEM (rejected before any API
spend), so tab traffic is always manufacturer-scoped. Every other caller (the
"search the web" button, tests) leaves it blank and gets the historical
auto-detect behavior -- the only path that still uses cross-decoder detect()
and the alternates/tie-band rendering.

Auto-detect precedence (see decoders/registry.py + oem_alias.py):
  0. find_exact on the WHOLE raw string (a model containing a brand word,
     e.g. "Club Car Carryall", wins before any OEM stripping).
  1. split_oem() pulls an optional leading OEM word off the query.
  2. find_exact on the remainder, filtered to that OEM.
  3. Scoped decoders.detect(..., manufacturer=oem). If that OEM has no
     decoder on file, note it (still falls through to the DB, and -- unlike
     a decoded serial -- on to the web, since a named-but-undecoded OEM is
     exactly the "look up a model" case, not a serial case).
  4. No OEM given -> detect() across every decoder.
  5. find_in_db (loose match), OEM-filtered when one was given.
  6. Web fallback -- auto-fires only for a plausible query with no
     confident (>=0.60) offline decode; otherwise offers a button. Picks
     between two strategies (see _looks_like_model): a serial-shaped query
     gets web_identify.identify_from_serial (unverified manufacturer/model/
     year guess); a model-shaped query gets web_identify.find_spec_sheet
     (the original full-spec extraction flow). These are different searches
     for different questions -- a serial search asks "what truck is this?",
     a model search asks "what are this truck's specs?" -- so conflating
     them lost most of the specs fields for a model lookup.
"""
from __future__ import annotations

import re
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Forklift
from ..schemas import ForkliftSpecs, LookupResult, SerialAlternate, SerialWebGuess
from . import web_identify
from .decoders.registry import DECODERS, TIE_BAND, detect, normalize
from .oem_alias import split_oem
from .serial_summary import summarize

WEB_CONFIDENCE_FLOOR = 0.60
_DECODER_LABELS = {"hyster_yale": "Hyster/Yale", "doosan": "Doosan"}
_LEAD_LETTERS_RE = re.compile(r"^[A-Za-z]{1,4}")


def find_exact(db: Session, query: str, manufacturer: str | None = None) -> Forklift | None:
    """Exact match on model, or 'manufacturer model' combined."""
    q = (query or "").strip().lower()
    if not q:
        return None
    combined = func.lower(Forklift.manufacturer + " " + Forklift.model)
    qry = db.query(Forklift).filter((func.lower(Forklift.model) == q) | (combined == q))
    if manufacturer:
        qry = qry.filter(Forklift.manufacturer == manufacturer)
    return qry.first()


def find_in_db(db: Session, query: str, manufacturer: str | None = None) -> Forklift | None:
    """Loose match on 'manufacturer model' or just model text."""
    q = (query or "").strip().lower()
    if not q:
        return None
    combined = func.lower(Forklift.manufacturer + " " + Forklift.model)
    qry = db.query(Forklift).filter(
        func.lower(Forklift.model).like(f"%{q}%") | combined.like(f"%{q}%")
    )
    if manufacturer:
        qry = qry.filter(Forklift.manufacturer == manufacturer)
    return qry.first()


def _existing_result(existing: Forklift) -> LookupResult:
    return LookupResult(
        found=True,
        already_in_db=True,
        existing_id=existing.id,
        specs=ForkliftSpecs(**{
            k: v for k, v in existing.as_dict().items()
            if k in ForkliftSpecs.model_fields
        }),
        source_url=existing.source_url,
        pdf_url=existing.pdf_url,
        message=f"Already in the database (record #{existing.id}).",
    )


def _has_decoder(manufacturer: str) -> bool:
    return any(manufacturer in d.manufacturers for d in DECODERS)


def _decoder_for(name: str):
    return next(d for d in DECODERS if d.name == name)


def _looks_like_model(db: Session, text: str) -> bool:
    """Which web strategy to use (auto-detect mode only): does this read as
    an OEM MODEL number rather than a serial? A leading digit (Toyota-style
    '8FGCU25') or a 1-4 letter lead that matches an EXISTING catalog model
    prefix (e.g. Crown's 'FC ####') reads as a model -- checked against the
    real catalog rather than guessed, since e.g. a bare 2-letter prefix is
    structurally identical between a real model code and a made-up serial.
    Anything else is treated as serial-shaped."""
    t = (text or "").strip()
    if not t:
        return False
    if t[0].isdigit():
        return True
    m = _LEAD_LETTERS_RE.match(t)
    if not m:
        return False
    return db.query(Forklift).filter(Forklift.model.ilike(f"{m.group(0)}%")).first() is not None


def _with_summary(serial):
    serial.summary, serial.summary_note = summarize(serial)
    return serial


def _serial_lookup_result(db: Session, candidates, norm: str) -> LookupResult:
    """Render the winning candidate, plus any alternate within the tie band
    -- never silently drop a close second."""
    best = candidates[0]
    decoder = _decoder_for(best.decoder)
    serial = _with_summary(decoder.decode(db, norm))

    alternates = []
    for c in candidates[1:]:
        if best.confidence - c.confidence > TIE_BAND:
            break
        alt_decoder = _decoder_for(c.decoder)
        alternates.append(SerialAlternate(decoder=c.decoder, serial=_with_summary(alt_decoder.decode(db, norm))))

    label = _DECODER_LABELS.get(best.decoder, best.decoder.replace("_", " ").title())
    kind_suffix = serial.kind
    if kind_suffix.startswith(best.decoder + "_"):
        kind_suffix = kind_suffix[len(best.decoder) + 1:]
    return LookupResult(
        found=True,
        kind="serial",
        serial=serial,
        alternates=alternates,
        message=f"Decoded as a {label} {kind_suffix.replace('_', ' ')}.",
    )


def _guess_to_specs(guess: SerialWebGuess) -> ForkliftSpecs:
    return ForkliftSpecs(
        manufacturer=guess.manufacturer, series=guess.model_family, model=guess.model,
        year_start=guess.year_start, year_end=guess.year_end,
    )


def _serial_web_result(raw: str, manufacturer: str | None) -> LookupResult:
    guess = web_identify.identify_from_serial(raw, manufacturer)
    has_info = bool(guess.manufacturer or guess.model)
    return LookupResult(
        found=has_info,
        kind="web",
        web_guess=guess,
        specs=_guess_to_specs(guess) if has_info else None,
        source_url=guess.source_url,
        message=("From a web search — unverified. Review before saving." if has_info
                  else "Searched the web but couldn't identify it confidently."),
    )


def _spec_sheet_web_result(raw: str) -> LookupResult:
    r = web_identify.find_spec_sheet(raw)
    if not r.found:
        return LookupResult(
            found=False, source_url=r.source_url, pdf_url=r.pdf_url,
            pdf_status=r.pdf_status, pdf_note=r.pdf_note, pages_tried=r.pages_tried,
            message=(f"Searched {r.pages_tried} page{'s' if r.pages_tried != 1 else ''} but none "
                      "listed specs for this model. Check the source link or try the model family."
                      if r.pages_tried else "No spec sheet found online."),
        )
    return LookupResult(
        found=True, kind="web", specs=r.specs, source_url=r.source_url, pdf_url=r.pdf_url,
        pdf_status=r.pdf_status, pdf_note=r.pdf_note, field_sources=r.field_sources,
        message="Found online. Review the specs below and confirm to save.",
    )


def _web_gate(raw: str, web: bool, best_confidence: float,
              run: Callable[[], LookupResult]) -> LookupResult:
    """Shared junk-gate / auto-fire / offer-button wrapper, independent of
    which web strategy `run` performs -- same rules for every mode."""
    if not settings.web_lookup_enabled:
        return LookupResult(found=False, message="Not in the database. Web lookup is disabled — "
                                                  "add ANTHROPIC_API_KEY and TAVILY_API_KEY to .env "
                                                  "to enable it.")
    if web:
        # Explicit user click (POST /lookup, web=1). Still junk-gated -- never
        # spend a credit on garbage just because a button was clicked.
        if not web_identify.is_plausible_equipment_query(raw):
            return LookupResult(found=False, message="That doesn't look like a serial number or "
                                                      "model — not searching the web for it.")
        return run()

    if web_identify.should_auto_fire(raw, best_confidence):
        return run()

    if web_identify.is_plausible_equipment_query(raw):
        return LookupResult(found=False, offer_web_search=True, web_query=raw,
                             message="Not found offline. You can search the web for it.")

    return LookupResult(found=False, message="Not in the database, and this doesn't look like a "
                                              "serial number or model — nothing to search for.")


def _lookup_auto(db: Session, query: str, web: bool) -> LookupResult:
    exact = find_exact(db, query)
    if exact:
        return _existing_result(exact)

    oem, rest = split_oem(query)
    scoped = bool(oem and rest.strip())
    search_text = rest if scoped else query

    if scoped:
        exact2 = find_exact(db, rest, manufacturer=oem)
        if exact2:
            return _existing_result(exact2)

    oem_note = None
    candidates = []
    if scoped:
        if _has_decoder(oem):
            candidates = detect(db, normalize(rest), manufacturer=oem)
        else:
            oem_note = f"No serial scheme on file for {oem} yet — searched the catalog by text instead."
    else:
        candidates = detect(db, normalize(query))

    if candidates and candidates[0].confidence >= WEB_CONFIDENCE_FLOOR:
        result = _serial_lookup_result(db, candidates, normalize(search_text))
        result.oem_note = oem_note
        return result

    existing = find_in_db(db, search_text, manufacturer=oem if scoped else None)
    if existing:
        return _existing_result(existing)

    manufacturer = oem if scoped else None
    if candidates:
        # A weak (<0.60) offline decode exists -- show it, and also offer/run
        # the web step since nothing reached confident territory.
        weak = _serial_lookup_result(db, candidates, normalize(search_text))
        weak.oem_note = oem_note
        web_result = _web_gate(query, web, candidates[0].confidence,
                                lambda: _pick_web_result(db, query, search_text, manufacturer))
        weak.web_guess = web_result.web_guess
        weak.offer_web_search = web_result.offer_web_search
        weak.web_query = web_result.web_query
        if web_result.found:
            weak.specs = web_result.specs
            weak.source_url = web_result.source_url or weak.source_url
            weak.pdf_url = web_result.pdf_url or weak.pdf_url
            weak.message = weak.message + " " + web_result.message
        return weak

    web_result = _web_gate(query, web, 0.0,
                            lambda: _pick_web_result(db, query, search_text, manufacturer))
    web_result.oem_note = oem_note
    return web_result


def _pick_web_result(db: Session, raw: str, shape_text: str, manufacturer: str | None) -> LookupResult:
    if _looks_like_model(db, shape_text):
        return _spec_sheet_web_result(raw)
    return _serial_web_result(raw, manufacturer)


_RECOGNISED = "Recognised: Toyota, Hyster, Yale, CAT/Mitsubishi, Crown, Clark, Raymond, Komatsu and others."
_OEM_REQUIRED_MESSAGE = (
    "Start with the manufacturer, for example “Toyota 8FGCU25”. A bare model number "
    "can be ambiguous across brands. " + _RECOGNISED
)
_OEM_REQUIRED_SERIAL_MESSAGE = (
    "Start with the manufacturer, for example “Hyster G004V01515D” or "
    "“Doosan L7-00116”. The same serial format can mean different trucks for "
    "different brands. " + _RECOGNISED
)


def _lookup_serial(db: Session, query: str, web: bool) -> LookupResult:
    """Serial tab: the OEM is REQUIRED (rejected before any DB or API work),
    which scopes the decoders to that brand. Decode-or-fail: no loose model
    text search -- a serial that doesn't decode goes straight to the
    serial-identify web fallback."""
    oem, rest = split_oem(query)
    if not (oem and rest.strip()):
        return LookupResult(found=False, message=_OEM_REQUIRED_SERIAL_MESSAGE)

    exact = find_exact(db, query)
    if exact:
        return _existing_result(exact)

    exact2 = find_exact(db, rest, manufacturer=oem)
    if exact2:
        return _existing_result(exact2)

    oem_note = None
    candidates = []
    if _has_decoder(oem):
        candidates = detect(db, normalize(rest), manufacturer=oem)
    else:
        oem_note = f"No serial scheme on file for {oem} yet."

    if candidates and candidates[0].confidence >= WEB_CONFIDENCE_FLOOR:
        result = _serial_lookup_result(db, candidates, normalize(rest))
        result.oem_note = oem_note
        return result

    if candidates:
        weak = _serial_lookup_result(db, candidates, normalize(rest))
        weak.oem_note = oem_note
        web_result = _web_gate(query, web, candidates[0].confidence,
                                lambda: _serial_web_result(query, oem))
        weak.web_guess = web_result.web_guess
        weak.offer_web_search = web_result.offer_web_search
        weak.web_query = web_result.web_query
        if web_result.found:
            weak.specs = web_result.specs
            weak.message = weak.message + " " + web_result.message
        return weak

    web_result = _web_gate(query, web, 0.0, lambda: _serial_web_result(query, oem))
    web_result.oem_note = oem_note
    return web_result


def _lookup_model(db: Session, query: str, web: bool) -> LookupResult:
    """Model tab: the OEM is REQUIRED (rejected before any DB or API work),
    then exact match -> loose DB match -> spec-sheet web path. No serial
    decoding at all."""
    oem, rest = split_oem(query)
    if not (oem and rest.strip()):
        return LookupResult(found=False, message=_OEM_REQUIRED_MESSAGE)

    exact = find_exact(db, query)
    if exact:
        return _existing_result(exact)

    exact2 = find_exact(db, rest, manufacturer=oem)
    if exact2:
        return _existing_result(exact2)

    existing = find_in_db(db, rest, manufacturer=oem)
    if existing:
        return _existing_result(existing)

    return _web_gate(query, web, 0.0, lambda: _spec_sheet_web_result(query))


def lookup(db: Session, query: str, web: bool = False, mode: str = "") -> LookupResult:
    if mode == "serial":
        return _lookup_serial(db, query, web)
    if mode == "model":
        return _lookup_model(db, query, web)
    return _lookup_auto(db, query, web)
