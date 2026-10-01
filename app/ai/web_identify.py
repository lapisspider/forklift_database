"""Web fallback for a query that didn't resolve offline -- two strategies,
both gated by the same junk filter and TTL cache:
  - identify_from_serial: a serial that didn't decode (or decoded weakly).
    Never claims more than the excerpts support; always unverified.
  - find_spec_sheet: a MODEL query with no catalog match. This is the
    original "look up a forklift and add it" flow (full spec extraction),
    unrelated to serial decoding -- lookup.py picks between the two based on
    whether the query looks like a serial or a model number.
Both are gated so garbage input never spends a Tavily/Claude credit, and
cached for 10 minutes so a repeated query doesn't re-spend one either."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..config import settings
from ..schemas import ForkliftSpecs, SerialWebGuess
from . import extractor, tavily_client
from .oem_alias import OEM_DOMAINS, split_oem
from .pdf_verify import verify_pdf_url

_ALLOWED_CHARS_RE = re.compile(r"^[A-Za-z0-9 ,./-]+$")
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_ALPHA_REPEAT_RE = re.compile(r"([A-Za-z])\1{3,}")  # same LETTER 4+ times in a row --
# digits are deliberately excluded: a zero/nine-padded sequence (e.g. "ZZ-99999")
# is a normal-looking serial, not junk, so only letter-mashing counts here.

_CACHE_TTL = 600  # 10 minutes
_cache: dict[str, tuple[float, object]] = {}


@dataclass
class SpecSheetResult:
    found: bool
    specs: ForkliftSpecs | None = None
    source_url: str | None = None
    pdf_url: str | None = None
    pdf_status: int | None = None
    pdf_note: str | None = None
    pages_tried: int = 0
    field_sources: dict[str, str] = field(default_factory=dict)


def is_plausible_equipment_query(raw: str) -> bool:
    """Junk gate: reject before a single API call is made. All of these must
    hold, or the query is treated as garbage."""
    text = (raw or "").strip()
    if not (3 <= len(text) <= 40):
        return False
    if not _ALLOWED_CHARS_RE.match(text):
        return False

    alnum = _ALNUM_RE.sub("", text)
    if alnum and len(set(alnum)) == 1:
        return False  # e.g. "1111", "aaaa" -- no structure at all, just one char repeated
    if _ALPHA_REPEAT_RE.search(text):
        return False  # e.g. "aaaa1234" -- keyboard-mashed letters

    oem, rest = split_oem(text)
    has_digit = any(c.isdigit() for c in text)
    if not has_digit and not oem:
        return False

    tokens = (rest if oem else text).split()
    if len(tokens) > 3:
        return False

    if text.isalpha() and not has_digit and not oem:
        return False

    return True


def should_auto_fire(raw: str, best_confidence: float) -> bool:
    """True when the web search should run WITHOUT asking the user first."""
    return (
        settings.web_lookup_enabled
        and best_confidence < 0.60
        and is_plausible_equipment_query(raw)
    )


def _cache_key(raw: str, manufacturer: str | None) -> str:
    norm_raw = re.sub(r"\s+", " ", raw.strip().lower())
    return f"{(manufacturer or '').lower()}|{norm_raw}"


def _cache_get(key: str):
    cached = _cache.get(key)
    if cached and (time.time() - cached[0]) < _CACHE_TTL:
        return cached[1]
    return None


def identify_from_serial(raw: str, manufacturer: str | None = None) -> SerialWebGuess:
    """Search the web for what truck a serial belongs to. Never raises --
    a failure just comes back as a low-confidence empty guess."""
    key = f"serial|{_cache_key(raw, manufacturer)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    if manufacturer:
        query = f'{manufacturer} forklift serial number "{raw}" model year'
    else:
        query = f'forklift serial number "{raw}" what model manufacturer'

    try:
        search = tavily_client.answer_search(query)
    except Exception:  # noqa: BLE001 -- best-effort, never fatal
        guess = SerialWebGuess(confidence="low", reasoning="Web search failed.")
        _cache[key] = (time.time(), guess)
        return guess

    results = search.get("results", [])
    answer = search.get("answer") or ""
    content = (("AI SEARCH ANSWER:\n" + answer + "\n\n") if answer else "") + "\n\n".join(
        (r.get("raw_content") or r.get("content") or "") for r in results[:3]
    )
    content = content[:15_000]
    source_url = results[0].get("url") if results else None

    guess = extractor.identify_from_serial(raw, manufacturer, content, source_url)
    _cache[key] = (time.time(), guess)
    return guess


_MODEL_RE = re.compile(r"^([A-Za-z0-9]*?)(\d+)([A-Za-z]*)$")
_RANGE_RE = re.compile(r"([A-Za-z0-9]*?)(\d+)\s*[-–/]\s*(\d+)([A-Za-z]*)")
_FUELS = {"Electric", "Diesel", "LPG", "Gasoline", "Gasoline/LPG", "Diesel/LPG"}
_TRACKED = ("manufacturer", "model", "series", "truck_class", "year_start", "year_end",
            "capacity_kg", "fuel_type", "chassis")
_MAX_VERIFY_PER_SEARCH = 4
_MAX_EXTRACT_ATTEMPTS = 4
_NON_ENGLISH_RE = re.compile(r"portugu|brazil|spanish|espanol|french|german|italian|dutch|russian|chinese|polish|turkish", re.I)


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _pdf_level(text: str, model: str, series: str | None) -> int:
    """2 = covers this exact model (named, or inside a printed range such as
    H40-70FT); 1 = names only its series/family; 0 = unrelated."""
    norm = _alnum(text)
    mt = _alnum(model)
    if mt and mt in norm:
        return 2
    mm = _MODEL_RE.match(mt)
    if mm:
        prefix, num, suffix = mm.group(1), int(mm.group(2)), mm.group(3)
        for r in _RANGE_RE.finditer(text.lower()):
            if (r.group(1) == prefix and r.group(4) == suffix
                    and int(r.group(2)) <= num <= int(r.group(3))):
                return 2
    st = _alnum(series or "")
    if len(st) >= 4 and st in norm:
        return 1
    return 0


class _PdfFinder:
    """Collects PDF links that appeared in search results and keeps only ones
    that pass a live HTTP check. Never builds or guesses a URL."""

    def __init__(self, name: str, model: str, domains: list[str] | None):
        self.name, self.model, self.domains = name, model, domains
        self.searched: list[str] = []
        self.tried: dict[str, int | None] = {}
        self.model_pdf: tuple[str, int] | None = None
        self.family_pdf: tuple[str, int] | None = None
        self.first_page: str | None = None
        self._model_rounds_done = False

    def consider(self, search: dict, series: str | None) -> None:
        for r in search.get("results", []):
            self.first_page = self.first_page or r.get("url")
        cands = []
        for r in tavily_client.pdf_urls(search):
            url = r["url"]
            if url in self.tried:
                continue
            body = (r.get("raw_content") or r.get("content") or "")[:30_000]
            level = _pdf_level(f"{url} {r.get('title') or ''} {body}", self.model, series)
            if level:
                cands.append((level, url))
        cands.sort(key=lambda c: (-c[0], bool(_NON_ENGLISH_RE.search(c[1]))))
        for level, url in cands[:_MAX_VERIFY_PER_SEARCH]:
            status = verify_pdf_url(url)
            self.tried[url] = status
            if status is None:
                continue
            if level == 2:
                self.model_pdf = (url, status)
                return
            self.family_pdf = self.family_pdf or (url, status)

    def search(self, query: str, series: str | None, domains: list[str] | None = None) -> None:
        self.searched.append(query)
        try:
            self.consider(tavily_client.search_pdf(query, include_domains=domains), series)
        except Exception:  # noqa: BLE001 -- escalation is best-effort
            pass

    def model_rounds(self, series: str | None) -> None:
        if self._model_rounds_done:
            return
        self._model_rounds_done = True
        rounds = [(f"{self.name} spec sheet filetype:pdf", None),
                  (f"{self.name} technical guide", None),
                  (f"{self.name} brochure pdf", None)]
        if self.domains:
            rounds.append((f"{self.name} specifications pdf", self.domains))
        for query, domains in rounds:
            if self.model_pdf:
                return
            self.search(query, series, domains)

    def family_rounds(self, oem_text: str, series: str) -> None:
        """Spec sheets usually cover a range, so retry on the series name."""
        base = f"{oem_text} {series}".strip()
        rounds = [(f"{base} spec sheet filetype:pdf", None)]
        if self.domains:
            rounds.append((f"{base} specifications pdf", self.domains))
        for query, domains in rounds:
            if self.model_pdf:
                return
            self.search(query, series, domains)

    def best(self) -> tuple[str, int, bool] | None:
        """(url, http_status, covers_exact_model)"""
        if self.model_pdf:
            return (*self.model_pdf, True)
        if self.family_pdf:
            return (*self.family_pdf, False)
        return None


def _rank_results(results: list[dict], domains: list[str] | None, model: str) -> list[dict]:
    """Order pages to try for extraction: the OEM's own site first, then pages
    whose URL/title names the model, then the rest in search-rank order."""
    mt = _alnum(model)

    def key(item):
        i, r = item
        host = (urlparse(r.get("url") or "").hostname or "").lower()
        oem_site = any(host == d or host.endswith("." + d) for d in domains or [])
        names_model = bool(mt) and mt in _alnum(f"{r.get('url') or ''} {r.get('title') or ''}")
        return (not oem_site, not names_model, i)

    return [r for _, r in sorted(enumerate(results), key=key)]


def _page_content(result: dict | None) -> str:
    return (result or {}).get("raw_content") or (result or {}).get("content") or ""


def find_spec_sheet(raw: str) -> SpecSheetResult:
    """Original 'look up a forklift and add it' flow: search for a spec
    sheet, extract ForkliftSpecs, then fill every gap (series, class, years,
    capacity/fuel/chassis) and escalate until a verified PDF is found or the
    searches are exhausted. For a MODEL query, not a serial -- see
    identify_from_serial. Never raises."""
    key = f"specsheet|{_cache_key(raw, None)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached

    oem, rest = split_oem(raw)
    text = raw.strip()
    typed_oem = text[: len(text) - len(rest)].strip() if oem else ""
    model_text = rest if oem else text
    name = f"{typed_oem} {model_text}".strip()
    finder = _PdfFinder(name, model_text, OEM_DOMAINS.get(oem or ""))

    def finish(result: SpecSheetResult) -> SpecSheetResult:
        best = finder.best()
        if best:
            result.pdf_url, result.pdf_status = best[0], best[1]
            result.pdf_note = (
                f"PDF naming this model found and link-checked (HTTP {best[1]})." if best[2] else
                f"Link-checked PDF found (HTTP {best[1]}), but it covers the product family "
                f"rather than naming this exact model.")
        else:
            searched = "".join(f'; "{q}"' for q in finder.searched)
            result.pdf_note = (f'No spec-sheet PDF was found. Searched: "{name} forklift '
                               f'specifications spec sheet"{searched}.')
        result.source_url = result.source_url or finder.first_page
        _cache[key] = (time.time(), result)
        return result

    try:
        search = tavily_client.search_spec_sheet(raw)
    except Exception:  # noqa: BLE001 -- best-effort, never fatal
        return finish(SpecSheetResult(found=False))

    results = search.get("results", [])
    first_url = results[0].get("url") if results else None
    finder.consider(search, None)
    candidates = _rank_results(results, finder.domains, model_text)[:_MAX_EXTRACT_ATTEMPTS]
    if not candidates:
        finder.model_rounds(None)
        best = finder.best()
        if best:
            candidates = [{"url": best[0]}]

    specs = content = source_url = None
    tried = 0
    for r in candidates:
        tried += 1
        text_in = _page_content(r)
        if not text_in and r.get("url"):
            try:
                text_in = tavily_client.extract_url(r["url"])
            except Exception:  # noqa: BLE001
                text_in = ""
        if not text_in:
            continue
        got = extractor.extract_specs(text_in, hint=raw, published_years_only=True)
        if got.manufacturer or got.model:
            specs, content, source_url = got, text_in, r.get("url")
            break
    if specs is None:
        res = SpecSheetResult(found=False, source_url=first_url, pages_tried=tried)
        return finish(res)

    sources = {f: "spec_sheet" for f in _TRACKED if not _empty(getattr(specs, f))}
    if _empty(specs.manufacturer) and oem:
        specs.manufacturer, sources["manufacturer"] = oem, "query"
    if _empty(specs.model) and model_text:
        specs.model, sources["model"] = model_text, "query"
    mfr, model = specs.manufacturer or "", specs.model or raw

    if _empty(specs.series):
        series = extractor.find_series(mfr, model)
        if series:
            specs.series, sources["series"] = series, "web_search"

    if _empty(specs.truck_class):
        tc = extractor.find_truck_class(mfr, model)
        if tc:
            specs.truck_class, sources["truck_class"] = tc, "web_search"

    if specs.year_start is None and specs.year_end is None:
        ys, ye = extractor.find_production_years(mfr, model)
        if ys is not None or ye is not None:
            specs.year_start, specs.year_end = ys, ye
            sources["year_start"] = sources["year_end"] = "web_search"
        else:
            ys, ye = extractor.estimate_production_years(mfr, model, specs.series, content)
            if ys is not None or ye is not None:
                specs.year_start, specs.year_end = ys, ye
                sources["year_start"] = sources["year_end"] = "estimate"

    missing = [f for f in ("capacity_kg", "fuel_type", "chassis") if _empty(getattr(specs, f))]
    if missing:
        for f, v in extractor.find_core_specs(mfr, model, missing).items():
            if f == "fuel_type" and v not in _FUELS:
                continue
            setattr(specs, f, v)
            sources[f] = "web_search"

    if not finder.model_pdf:
        finder.model_rounds(specs.series)
    if not finder.model_pdf and not _empty(specs.series) and _alnum(specs.series) != _alnum(model_text):
        finder.family_rounds(typed_oem or mfr, specs.series)

    return finish(SpecSheetResult(found=True, specs=specs, source_url=source_url, pages_tried=tried,
                                  field_sources=sources))
