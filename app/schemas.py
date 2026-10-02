"""Pydantic schemas shared between the AI layer and the API."""
import re

from pydantic import BaseModel, Field, field_validator

_SERIES_WORD_RE = re.compile(r"\s*[-\s]?\bseries\b\.?", re.IGNORECASE)


def clean_series(value: str | None) -> str | None:
    """Strip the word "Series" from a series name.

    House convention: a series is stored bare -- "8", not "8-Series"; "FC 5700",
    not "FC 5700 series". Applied here rather than only in the extractor prompt so
    every producer (spec extraction, find_series, serial identification) is covered
    even when the model ignores the instruction.
    """
    if not value:
        return None
    cleaned = _SERIES_WORD_RE.sub("", value).strip(" -–—	")
    return cleaned or None


class ForkliftSpecs(BaseModel):
    """Structured specs extracted from a spec sheet. All fields optional."""
    manufacturer: str | None = Field(None, description="OEM / brand")
    series: str | None = Field(None, description="Manufacturer's model-family/range designation this model belongs to (e.g. 'E80-120XN', 'FC 5700', 'Fortis'). NEVER include the word 'Series': Toyota's 8-Series is '8'. Always fill unless truly indeterminable.")
    model: str | None = None
    year_start: int | None = Field(None, description="First production year of this model. If not stated, give a best-effort educated estimate.")
    year_end: int | None = Field(None, description="Last production year of this model; null if still in production. If unclear, give a best-effort estimate.")
    capacity_kg: float | None = Field(None, description="Rated load capacity in kilograms")
    fuel_type: str | None = Field(None, description="Canonical: Electric, Diesel, LPG, Gasoline, Gasoline/LPG, or Diesel/LPG")
    chassis: str | None = Field(None, description="Shared-chassis grouping/frame this model uses, if stated (e.g. 'Large-Capacity Frame')")
    truck_class: str | None = Field(None, description="OSHA powered-industrial-truck class as 'Class I'..'Class VII'")
    notes: str | None = None

    @field_validator("series")
    @classmethod
    def _strip_series_word(cls, v: str | None) -> str | None:
        return clean_series(v)


class SerialMatch(BaseModel):
    """A catalog forklift resolved from a decoded serial prefix's model family."""
    id: int
    manufacturer: str
    model: str


class DoosanPrefixRow(BaseModel):
    """One doosan_prefixes.json row rendered verbatim (a prefix can appear
    twice, e.g. 'FH-' -- both rows are shown, never merged)."""
    model: str
    power: str | None = None
    engine: str | None = None
    certification: str | None = None
    fuel_trans: str | None = None
    brake: str | None = None
    voltage: str | None = None
    system: str | None = None
    configuration: str | None = None


class SerialDecodeResult(BaseModel):
    """Offline decode of an OEM serial number, bare prefix, or pre-1995 Yale
    serial. Never touches the web on its own."""
    kind: str = "unrecognized"   # serial | prefix | unknown_prefix | yale_pre1995 |
                                  # doosan_serial | doosan_prefix | doosan_unknown_prefix | unrecognized
    prefix: str | None = None
    generation: str | None = None       # leading letter of the prefix (Hyster/Yale)
    family_digits: str | None = None    # last 3 chars of the prefix (Hyster/Yale)
    plant: str | None = None
    plant_location: str | None = None
    sequence: str | None = None
    year_code: str | None = None
    candidate_years: list[int] = []
    correction_note: str | None = None
    families: list[str] = []            # raw model-family text, one per matching source row
    matches: list[SerialMatch] = []
    twin_prefix: str | None = None
    twin_families: list[str] = []
    pre1995_eras: list[dict] = []
    notes: list[str] = []

    # ---- Doosan-specific ----
    doosan_rows: list[DoosanPrefixRow] = []   # power/fuel_trans/brake/... per prefix row
    year_note: str | None = None              # estimate_year()'s prose (approximate/at-or-before/etc)
    anomalies: list[str] = []                 # "Source anomaly: {flag}" strings, surfaced not silenced
    orphan: bool = False                      # prefix known only from the year table, no prefix-table row
    chassis_note: str | None = None           # fixed "leading letter is chassis family, not fuel" note
    year_basis: str | None = None             # approximate | range | at_or_before | single_marker
    year_values: list[int] = []               # the year(s) year_basis refers to

    # ---- Hyster/Yale-specific ----
    brand: str | None = None                  # Hyster or Yale, when the matched prefix rows agree
    family_guess: bool = False                # family inferred from digits only (prefix not on file)

    # ---- Lead sentence (app/ai/serial_summary.py) ----
    summary: str | None = None
    summary_note: str | None = None


class SerialAlternate(BaseModel):
    """A second (or third) decode that came within the tie band of the winner
    -- rendered alongside it, never silently dropped."""
    decoder: str
    serial: SerialDecodeResult


class SerialWebGuess(BaseModel):
    """Best-effort manufacturer/model guess from a web search on a serial that
    didn't decode offline. Always rendered as unverified; never auto-saved."""
    manufacturer: str | None = None
    model_family: str | None = None
    model: str | None = None
    year_start: int | None = None
    year_end: int | None = None
    confidence: str = "low"    # low | medium | high
    reasoning: str = ""
    source_url: str | None = None


class LookupResult(BaseModel):
    """What a web lookup returns before the user confirms saving."""
    found: bool
    already_in_db: bool = False
    existing_id: int | None = None
    specs: ForkliftSpecs | None = None
    source_url: str | None = None
    pdf_url: str | None = None
    pdf_status: int | None = None      # HTTP status from the link check on pdf_url
    pages_tried: int = 0               # pages run through extraction (spec-sheet lookups)
    pdf_note: str | None = None        # verified / family-level / "no PDF found, searched ..."
    field_sources: dict[str, str] = {}  # field -> spec_sheet | web_search | estimate | query (not rendered)
    message: str = ""
    kind: str = "web"                  # web | serial -- which partial to render
    serial: SerialDecodeResult | None = None
    alternates: list[SerialAlternate] = []
    oem_note: str | None = None        # e.g. "No serial scheme on file for Crown yet..."
    web_guess: SerialWebGuess | None = None
    offer_web_search: bool = False     # render the "Search the web for this serial" button
    web_query: str | None = None       # the raw query to resubmit with web=1


class NLQueryResult(BaseModel):
    """Result of a plain-English question against the database."""
    question: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    answer: str = ""
    error: str | None = None
