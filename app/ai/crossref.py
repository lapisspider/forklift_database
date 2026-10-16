"""Cross-market model lookup: "is the CAT DP18NTD sold in the US, and what is
it called here?"

This is the question the SQL path in nl_query.py cannot answer, for two
reasons: the asked model usually has no catalog row at all (so the SELECT
returns nothing), and "the same truck under another name" is a relationship no
column encodes.

The pipeline, in order:

  1. classify() -- is this a cross-market question? Anything else returns None
     and nl_query falls through to the normal text-to-SQL path. A question that
     a SELECT can answer must keep being answered by one.
  2. subject specs -- the catalog first, then the Lookup tab's own web pipeline
     (web_identify.find_spec_sheet) when there is no row. This is what gives us
     the asked truck's real capacity.
  3. extractor.find_market_equivalent() -- the web names candidate
     counterparts. Treat every name as a CLAIM, never an answer.
  4. each candidate goes back through SQL against the catalog, which is both how
     we return a real row (with its id and specs) and how we fact-check the
     claim: a candidate whose stored capacity is nowhere near the subject's is
     not the same truck. That check is what rejects the "a 1.8 t truck's US
     equivalent is the 4.0 t DP40K" class of answer.

A candidate is reported whatever the outcome -- named and explained even when it
is absent from the catalog or fails the capacity check. Suppressing it would
throw away the one useful part of the answer; the verdict says which kind of
answer it is, and `rows` stays empty so the table reads "no matching rows"
exactly as it does anywhere else.
"""
from __future__ import annotations

import re

from anthropic import Anthropic
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..schemas import EquivalentMatch, ForkliftSpecs, NLQueryResult
from . import extractor, web_identify
from .decoders.types import normalize
from .lookup import find_exact, find_in_db
from .oem_alias import split_oem

# How far a candidate's catalog capacity may sit from the subject's before we
# stop calling it the same truck. Deliberately loose: a genuine counterpart can
# carry a different nameplate rating in another market (the Hyundai BRP-9 rows
# in CLAUDE.md are exactly that), so this gates against a DIFFERENT SIZE of
# truck, not against two ratings that disagree.
CAPACITY_TOLERANCE = 0.20

# Hyster and Yale are one group and their trucks are often mechanically
# identical, so a cross-market search will happily answer a Yale question with
# a Hyster model. House rule (CLAUDE.md) is that kits must NOT transfer between
# Hyster/Yale twins even then, and the people asking this question are pricing
# parts -- so a counterpart that crosses the badge carries the warning with it.
_TWIN_BRANDS = {"Hyster", "Yale"}

_COLUMNS = ["id", "manufacturer", "series", "model", "capacity_kg",
            "fuel_type", "truck_class", "info_status"]
_SELECT = "SELECT " + ", ".join(_COLUMNS) + "\nFROM forklifts\n"

# Space- and case-insensitive, matching the house rule for kit SKUs: catalog
# model strings carry stray spaces ("ETR 230" against "ETR230").
# Both exclude the subject's own row. Without that, a candidate that is a
# PREFIX of the model we asked about matches it under the LIKE below -- a web
# search proposing "ERC040VA" as the counterpart of "ERC040VAN" resolved to the
# ERC040VAN row itself, compared its capacity against its own, and reported a
# confirmed counterpart. The guard is written as (:subj IS NULL OR ...) because
# a bare <> against a NULL subject would exclude every row.
_NOT_SUBJECT = ("\n  AND (:subj IS NULL OR REPLACE(LOWER(model), ' ', '') "
                "<> REPLACE(LOWER(:subj), ' ', ''))")
_EXACT_SQL = (_SELECT + "WHERE REPLACE(LOWER(model), ' ', '') = REPLACE(LOWER(:m), ' ', '')"
              + _NOT_SUBJECT)
_LOOSE_SQL = (_SELECT + "WHERE REPLACE(LOWER(model), ' ', '') LIKE REPLACE(LOWER(:m), ' ', '')"
              + _NOT_SUBJECT + "\nLIMIT 10")

# The backstop, for when the web names no counterpart at all. It names real
# catalog trucks of the same size and type, which is a different and weaker
# claim than "this is the same truck" -- `rows_are_nearest` makes the answer
# and the table say so. Ordering: same OSHA class first (which is also the
# cushion/pneumatic distinction for IC trucks), then same fuel, then same OEM,
# then nearest capacity.
_NEAREST_SQL = _SELECT + """WHERE capacity_kg IS NOT NULL
  AND capacity_kg BETWEEN :lo AND :hi
ORDER BY (truck_class = :cls) DESC,
         (LOWER(fuel_type) = LOWER(:fuel)) DESC,
         (manufacturer = :mfr) DESC,
         ABS(capacity_kg - :cap)
LIMIT 8"""


class CrossRefIntent(BaseModel):
    is_crossref: bool = False
    manufacturer: str | None = None
    model: str | None = None
    target_market: str = "the United States"


_CLASSIFY_SYSTEM = """You sort one forklift question into one of two buckets.

"crossref" -- the question is about a model's MARKET: whether a model is sold in
a particular country or region, what it is called there, or what the equivalent
model there is. Examples: "is the CAT DP18NTD offered in the US?", "what is the
American version of a Linde H25D?", "is this a European model?".

"sql" -- everything else, including every question about the contents of a
catalog: counts, filters, which trucks are Class I, which kits fit a model,
capacities, what is in the database. If a question could be answered by querying
a table of forklifts, it is "sql".

A question naming a model but asking about its specs, its kits or whether it is
in the database is "sql", not "crossref". Only a question about MARKET or REGION
is "crossref"."""


def _client() -> Anthropic:
    return Anthropic(api_key=settings.anthropic_api_key)


def classify(question: str) -> CrossRefIntent:
    """Is this a cross-market question, and about which model? Never raises --
    on any failure it reports "not crossref", so the SQL path still runs."""
    tool = {
        "name": "record_intent",
        "description": "Classify a forklift question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "bucket": {"type": "string", "enum": ["crossref", "sql"]},
                "manufacturer": {
                    "type": ["string", "null"],
                    "description": "OEM/brand named in the question, e.g. 'CAT'. Null if none.",
                },
                "model": {
                    "type": ["string", "null"],
                    "description": ("The model designation the question is about, e.g. "
                                    "'DP18NTD'. Model only -- no brand, no prose."),
                },
                "target_market": {
                    "type": "string",
                    "description": ("The market being asked about, as a phrase like 'the "
                                    "United States'. Default to 'the United States'."),
                },
            },
            "required": ["bucket"],
        },
    }
    try:
        resp = _client().messages.create(
            model=settings.claude_model,
            max_tokens=200,
            system=_CLASSIFY_SYSTEM,
            tools=[tool],
            tool_choice={"type": "tool", "name": "record_intent"},
            messages=[{"role": "user", "content": question}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "record_intent":
                d = block.input
                return CrossRefIntent(
                    is_crossref=(d.get("bucket") == "crossref"),
                    manufacturer=d.get("manufacturer") or None,
                    model=d.get("model") or None,
                    target_market=d.get("target_market") or "the United States",
                )
    except Exception:  # noqa: BLE001 -- classification is advisory; fall back to SQL
        return CrossRefIntent()
    return CrossRefIntent()


def _row_specs(row) -> ForkliftSpecs:
    return ForkliftSpecs(
        manufacturer=row.manufacturer, series=row.series, model=row.model,
        capacity_kg=row.capacity_kg, fuel_type=row.fuel_type, truck_class=row.truck_class,
    )


def _subject_specs(db: Session, name: str, oem: str | None,
                   model: str) -> tuple[ForkliftSpecs | None, str | None, bool]:
    """The asked truck's own specs: catalog first, then the web.

    Returns (specs, source_url, in_catalog). The capacity in here is what every
    candidate below is measured against, so an unknown capacity is worth a web
    search even though the question asked is not "what are its specs".
    """
    row = find_exact(db, name) or find_exact(db, model, manufacturer=oem)
    if row is None:
        # Loose match whether or not a brand was named. Gating this on `oem`
        # meant a question that never said "Yale" ("what is the European
        # equivalent of the ERC040VA?") skipped the catalog entirely and paid
        # the web for a truck already on file -- it came back "ERC040VA,
        # 1814 kg, not in this database" when row 209 is ERC040VAN at 1800 kg.
        row = find_in_db(db, model, manufacturer=oem)
    if row is not None:
        return _row_specs(row), row.source_url, True

    if not settings.web_lookup_enabled:
        return None, None, False
    if not web_identify.is_plausible_equipment_query(name):
        return None, None, False
    sheet = web_identify.find_spec_sheet(name)
    if sheet.found and sheet.specs:
        return sheet.specs, sheet.source_url, False
    return None, sheet.source_url, False


def _check(candidate: str, row, subject_cap: float | None,
           exact: bool = True) -> EquivalentMatch:
    """Turn one web-proposed name, plus its catalog row or absence, into a verdict."""
    if row is None:
        return EquivalentMatch(
            model=candidate, in_catalog=False, verdict="not_in_catalog",
            note="Named by the web search, but there is no row for it in this catalog.",
        )
    m = EquivalentMatch(
        model=candidate, forklift_id=row.id, manufacturer=row.manufacturer,
        capacity_kg=row.capacity_kg, fuel_type=row.fuel_type, truck_class=row.truck_class,
        in_catalog=True, verdict="confirmed" if exact else "near_match",
    )
    if not exact:
        m.note = (f"No row for {candidate}. The nearest model string in the catalog is "
                  f"{row.model}, which is a different order code — the suffix decides kit "
                  f"fit, so confirm this before relying on it.")
    if subject_cap and row.capacity_kg:
        drift = abs(row.capacity_kg - subject_cap) / subject_cap
        if drift > CAPACITY_TOLERANCE:
            m.verdict = "capacity_mismatch"
            m.note = (f"Rated {row.capacity_kg:,.0f} kg against the asked model's "
                      f"{subject_cap:,.0f} kg, {drift:.0%} apart. That is a different size "
                      f"of truck, not the same model renamed.")
    elif not subject_cap:
        m.verdict = "unchecked"
        m.note = "No capacity on file for the asked model, so this could not be checked."
    return m


# A range/series designation: "ERC030-040VA", "E30-40XN", "J1.6-2.0XN".
_RANGE_RE = re.compile(r"^([A-Za-z]*)(\d+(?:\.\d+)?)\s*[-–/]\s*(\d+(?:\.\d+)?)([A-Za-z0-9]*)$")


def _expand_designation(candidate: str) -> list[str]:
    """A designation plus, for a range, the concrete codes it covers.

    Yale publishes the ERC030-040VA spec sheet for two trucks, ERC030VA and
    ERC040VA, and a search for a model's foreign name readily answers with that
    series name. Expanding it is what lets the same-designation check see that
    it covers the truck we asked about.
    """
    out = [candidate]
    m = _RANGE_RE.match(candidate.strip())
    if m:
        prefix, lo, hi, suffix = m.groups()
        out += [f"{prefix}{lo}{suffix}", f"{prefix}{hi}{suffix}"]
    return out


def _is_same_designation(candidate: str, subject_model: str | None) -> bool:
    """Is this "counterpart" just the asked model's own designation again?

    A search for a model's foreign name readily proposes the same code with a
    suffix added or dropped, or the series name covering it -- asked for the
    European name of a Yale ERC040VAN it answered "ERC040VA", then
    "ERC030-040VA". Neither is a European model: the first is the same code
    without the order-code suffix the catalog stores, the second is Yale's own
    North American series sheet. Treating either as a cross-market counterpart
    invents a regional distinction that is not there.
    """
    if not subject_model:
        return False
    s = normalize(subject_model)
    if not s:
        return False
    for form in _expand_designation(candidate):
        c = normalize(form)
        if c and (c in s or s in c):
            return True
    return False


def _lookup_candidate(db: Session, candidate: str, subject_model: str | None):
    """The SQL half: a web-named model is only an answer once the catalog has
    been asked about it. Exact match first, then a contains-match.

    Returns (row, sql, exact). `exact` matters: house convention is that a
    model name is the FULL order code, suffix included, because the suffix
    identifies the engine/ECU and decides kit fit. So a contains-match found a
    DIFFERENT truck whose code merely overlaps, and must never be reported with
    the same confidence as an exact hit.
    """
    params = {"m": candidate, "subj": subject_model}
    row = db.execute(text(_EXACT_SQL), params).first()
    if row is not None:
        return row, _EXACT_SQL, True
    row = db.execute(text(_LOOSE_SQL), {**params, "m": f"%{candidate}%"}).first()
    if row is None:
        return None, _EXACT_SQL, False
    return row, _LOOSE_SQL, False


def _nearest_in_catalog(db: Session, specs: ForkliftSpecs):
    """Catalog trucks of the same size and type as the asked model.

    Not an equivalence claim -- these are the nearest things this catalog
    holds, which is worth naming when the web named nothing. Returns
    ([], None) when the asked model has no capacity to measure against.
    """
    if not specs or not specs.capacity_kg:
        return [], None
    cap = specs.capacity_kg
    params = {
        "lo": cap * (1 - CAPACITY_TOLERANCE), "hi": cap * (1 + CAPACITY_TOLERANCE),
        "cap": cap, "cls": specs.truck_class, "fuel": specs.fuel_type,
        "mfr": specs.manufacturer,
    }
    rows = db.execute(text(_NEAREST_SQL), params).fetchall()
    return rows, _NEAREST_SQL


def _fallback_answer(result: NLQueryResult) -> str:
    """Plain-English answer assembled without Claude, for when the summary call
    fails. Never claims more than was actually established."""
    subject = result.subject
    name = (subject.model if subject else None) or result.question
    named = [m.model for m in result.matches]
    nearest = (" The closest trucks in this database by capacity and type are listed "
               "below; they are not confirmed equivalents.") if result.rows_are_nearest else ""
    if not named:
        return f"The web search did not name a counterpart for {name}.{nearest}"
    confirmed = [m.model for m in result.matches if m.verdict == "confirmed"]
    if confirmed:
        return (f"{name} corresponds to {', '.join(confirmed)}, which is in this "
                f"database.")
    return (f"The web search named {', '.join(named)} as the counterpart to {name}, "
            f"but there is no matching row in this database.{nearest}")


def _summarize(result: NLQueryResult) -> str:
    equiv = result.equivalent
    subject = result.subject
    lines = [f"Question: {result.question}", ""]
    if subject:
        where = ("Already in the catalog." if result.subject_in_catalog
                 else "NOT in the catalog; these specs came from a web search.")
        lines.append(
            f"Asked model: {subject.manufacturer or '?'} {subject.model or '?'} -- "
            f"capacity {subject.capacity_kg or 'unknown'} kg, "
            f"fuel {subject.fuel_type or 'unknown'}, "
            f"{subject.truck_class or 'class unknown'}. {where}"
        )
    else:
        lines.append("Asked model: its specs could not be established from the catalog "
                     "or the web.")
    if equiv:
        lines += [
            "",
            f"Web search says -- sold in the target market itself: {equiv.sold_in_target_market}",
            f"Markets it is sold in: {', '.join(equiv.home_markets) or 'not stated'}",
            f"Counterpart models proposed: {', '.join(equiv.candidate_models) or 'none'}",
            f"Its reasoning: {equiv.reasoning or 'none given'}",
        ]
    if result.matches:
        lines += ["", "Each proposed counterpart, after being looked up in the catalog:"]
        for m in result.matches:
            row_note = (f" (catalog row #{m.forklift_id}, {m.manufacturer}, "
                        f"{m.capacity_kg} kg)" if m.in_catalog else "")
            lines.append(f"  - {m.model}: {m.verdict}{row_note}"
                         + (f" -- {m.note}" if m.note else ""))

    if result.rows_are_nearest and result.rows:
        idx = {c: i for i, c in enumerate(result.columns)}
        lines += ["", "No counterpart was confirmed, so these are the trucks THIS CATALOG "
                      "holds at the same size and type. They are NOT counterparts and must "
                      "not be presented as the equivalent model:"]
        for row in result.rows[:8]:
            lines.append(
                f"  - {row[idx['manufacturer']]} {row[idx['model']]}: "
                f"{row[idx['capacity_kg']]} kg, {row[idx['fuel_type']]}, "
                f"{row[idx['truck_class']]}"
            )

    prompt = "\n".join(lines) + """

Write the answer in 2-4 plain sentences for a forklift parts dealer.

Requirements:
- NAME the counterpart model if one was proposed, even when it is not in the
  catalog and even when it failed the capacity check. Naming it is the useful
  part of the answer, so state it as the finding it is.
- A counterpart simply being absent from the catalog is an INVENTORY FACT about
  this database, not a doubt about the cross-reference. Write "the FD18NT is not
  in this database" and stop there. Never write "we cannot confirm it", "could
  not be verified", "we can't say directly" or any similar hedge about a
  counterpart that was named -- that reads as though the cross-reference itself
  is in question, which it is not. The capacity check below is the ONLY thing
  that casts doubt on a named counterpart.
- A model being absent from this catalog does NOT mean it is not sold in that
  market. Say "not in this database", never "not sold in the US", unless the web
  search itself established that.
- If a counterpart was rejected on capacity, say so and give both figures. Do
  not present it as the equivalent.
- A "near_match" is NOT a confirmation. It means the catalog has no row for the
  named model, only a different order code that overlaps its text. Say that the
  named model is not in the database and name the near match as a separate,
  different truck. Never write that the counterpart is "confirmed in the
  catalog" on the strength of a near_match.
- If no counterpart was confirmed but nearest-by-spec catalog trucks are listed,
  name two or three of them and say exactly what they are: the closest trucks in
  this database by capacity and type, not confirmed equivalents.
- If a note says two brands are badge twins whose kits do not transfer, carry
  that warning into your answer. The person asking is pricing parts.
- Invent nothing. Every model number you write must appear above."""
    resp = _client().messages.create(
        model=settings.claude_model, max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def answer(db: Session, question: str) -> NLQueryResult | None:
    """Answer a cross-market question, or return None if this is not one -- the
    caller then runs the normal SQL path."""
    intent = classify(question)
    if not intent.is_crossref or not intent.model:
        return None

    result = NLQueryResult(question=question, kind="crossref")
    oem, rest = split_oem(f"{intent.manufacturer or ''} {intent.model}".strip())
    model = rest or intent.model
    name = f"{oem or intent.manufacturer or ''} {model}".strip()

    if not settings.web_lookup_enabled:
        result.error = ("Cross-referencing a model between markets needs a web search. "
                        "Add TAVILY_API_KEY to .env to enable it.")
        return result

    result.subject, result.subject_source_url, result.subject_in_catalog = _subject_specs(
        db, name, oem, model)

    subject = result.subject
    hint = None
    if subject:
        hint = (f"{subject.capacity_kg or 'unknown'} kg, {subject.fuel_type or 'unknown fuel'}, "
                f"{subject.truck_class or 'class unknown'}")
    equiv = extractor.find_market_equivalent(
        oem or intent.manufacturer or "", model, intent.target_market, specs_hint=hint)
    result.equivalent = equiv

    subject_cap = subject.capacity_kg if subject else None
    statements: list[str] = []
    rows: list[list] = []
    subject_model = (subject.model if subject else None) or model
    for candidate in equiv.candidate_models[:5]:
        if _is_same_designation(candidate, subject_model):
            result.matches.append(EquivalentMatch(
                model=candidate, in_catalog=False, verdict="same_designation",
                note=(f"This is the asked model's own designation, not a counterpart — "
                      f"{candidate} and {subject_model} are the same code with the order-code "
                      f"suffix added or dropped, not two regional names."),
            ))
            continue
        row, sql, exact = _lookup_candidate(db, candidate, subject_model)
        statements.append(sql)
        match = _check(candidate, row, subject_cap, exact)
        if (subject and match.manufacturer
                and {subject.manufacturer, match.manufacturer} == _TWIN_BRANDS):
            warning = (f"{match.manufacturer} and {subject.manufacturer} are badge twins: "
                       f"kits do NOT transfer between them, even where the trucks are "
                       f"mechanically identical.")
            match.note = f"{match.note} {warning}" if match.note else warning
        result.matches.append(match)
        if row is not None and match.verdict == "confirmed":
            rows.append(list(row))

    if not rows and subject:
        # Nothing the web named survived (or it named nothing at all). Rather
        # than return a bare "no results", name the catalog's own trucks of
        # that size -- clearly flagged as nearest-by-spec, not as counterparts.
        near, near_sql = _nearest_in_catalog(db, subject)
        if near:
            rows = [list(r) for r in near]
            result.rows_are_nearest = True
            statements.append(near_sql)

    if statements:
        result.columns = _COLUMNS
        result.rows = rows
        # Rendered under "SQL used". One ran per candidate, hence the join.
        result.sql = ";\n".join(dict.fromkeys(statements))

    try:
        result.answer = _summarize(result)
    except Exception:  # noqa: BLE001 -- summary is best-effort
        result.answer = _fallback_answer(result)
    return result
