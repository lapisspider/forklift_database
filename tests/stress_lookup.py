"""Stress test for the lookup tool: adversarial input, edge cases, concurrency.

Run from the repo root:  .venv\\Scripts\\python.exe tests\\stress_lookup.py
Flags: -v for every passing check, --online to also check the OEM domains.

THE WHOLE SUITE IS FREE. Every billable call -- Tavily search and Anthropic
extraction -- is replaced with a function that raises, so no case can spend
money. That turns the block into a probe as well as a guard: a case declares
whether it should stay offline ("decode"/"refuse") or is allowed to reach the
web ("web"), and because the two web strategies enter through different Tavily
functions, the blocked call also proves WHICH strategy was chosen --
answer_search for serial identification, search_spec_sheet for model specs.

Sections:
  1. Junk gate          -- garbage must be refused before any API call
  2. OEM requirement    -- both tabs must reject a bare model/serial for free
  3. Serial decoding    -- malformed, boundary and unknown-prefix serials
  4. Normalisation      -- one serial in many spellings decodes identically
  5. Routing            -- model-vs-serial strategy, brand words in model names
  6. Catalog hits       -- known models resolve offline, never via the web
  7. Regression guards  -- the three defects fixed on 2026-10-01
  8. Concurrency        -- 200 parallel lookups across 20 threads
  9. Live domains       -- --online only: real HTTP against every OEM domain
"""
from __future__ import annotations

import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text as sql_text  # noqa: E402

from app.ai import extractor, lookup as L, tavily_client, web_identify  # noqa: E402
from app.ai.decoders.registry import detect  # noqa: E402
from app.ai.extractor import reconcile_capacity  # noqa: E402
from app.ai.oem_alias import OEM_DOMAINS, split_oem  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Forklift  # noqa: E402
from app.schemas import ForkliftSpecs, clean_series  # noqa: E402

ONLINE = "--online" in sys.argv
VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


# ---------------------------------------------------------------- scoreboard


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failures: list[tuple[str, str]] = []
        self.section = ""

    def start(self, name: str) -> None:
        self.section = name
        print(f"\n{'-' * 72}\n{name}\n{'-' * 72}")

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        if VERBOSE:
            print(f"  pass  {label}{('  -- ' + detail) if detail else ''}")

    def fail(self, label: str, detail: str) -> None:
        self.failures.append((f"{self.section.split(' --')[0]} :: {label}", detail))
        print(f"  FAIL  {label}\n        {detail}")

    def check(self, label: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.ok(label, detail)
        else:
            self.fail(label, detail or "condition was false")
        return bool(cond)

    def summary(self) -> int:
        total = self.passed + len(self.failures)
        print(f"\n{'=' * 72}")
        print(f"{self.passed}/{total} checks passed")
        if self.failures:
            print(f"\n{len(self.failures)} FAILING CHECK(S):")
            for label, detail in self.failures:
                print(f"  - {label}\n      {detail}")
        print("=" * 72)
        return 1 if self.failures else 0


R = Report()


# ------------------------------------------------- block every billable call


class PaidCall(RuntimeError):
    """Raised when a case tries to spend a credit. Carries the call name."""

    def __init__(self, fn: str, arg: str) -> None:
        super().__init__(f"{fn}({arg})")
        self.fn = fn
        self.arg = arg


_spend_log: list[str] = []
_spend_lock = threading.Lock()


def _blocked(name: str):
    def guard(*args, **kwargs):
        arg = str(args[0])[:70] if args else ""
        with _spend_lock:
            _spend_log.append(name)
        raise PaidCall(name, arg)
    return guard


def block_paid_calls() -> None:
    for fn in ("search_spec_sheet", "answer_search", "search_pdf", "extract_url"):
        setattr(tavily_client, fn, _blocked(f"tavily.{fn}"))
    extractor._client = _blocked("anthropic.messages")


class Outcome:
    """What a lookup did: a result, or the paid call it tried to make.

    `paid` is the Tavily/Anthropic function it reached for, which also names the
    strategy: tavily.answer_search = serial identification,
    tavily.search_spec_sheet = model spec extraction.
    """

    def __init__(self, result=None, paid: str | None = None, crash: str | None = None) -> None:
        self.result = result
        self.paid = paid
        self.crash = crash

    @property
    def free(self) -> bool:
        return self.paid is None and self.crash is None

    def describe(self) -> str:
        if self.crash:
            return f"CRASHED: {self.crash}"
        if self.paid:
            return f"went to the web via {self.paid}"
        r = self.result
        return (f"offline: found={r.found} in_db={r.already_in_db} "
                f"offer_web={r.offer_web_search} serial={bool(r.serial)}")


def run_lookup(db, query: str, mode: str = "", web: bool = False) -> Outcome:
    """Run a lookup and record whether it reached a billable call.

    The spend log, not a propagating exception, is what decides: web_identify
    wraps its search calls in `except Exception`, so a blocked call is swallowed
    there and the lookup returns a normal "nothing found" result. Watching the
    log catches the attempt either way.
    """
    with _spend_lock:
        mark = len(_spend_log)
    try:
        result = L.lookup(db, query, web=web, mode=mode)
        crash = None
    except PaidCall:
        result, crash = None, None
    except Exception:  # noqa: BLE001 -- a crash is a finding, not a stack trace
        result = None
        crash = traceback.format_exc(limit=2).strip().replace("\n", " | ")
    with _spend_lock:
        attempted = _spend_log[mark:]
    return Outcome(result=result, paid=attempted[0] if attempted else None, crash=crash)


def must_be_free(label: str, outcome: Outcome) -> bool:
    """A check that this case never reached a billable call."""
    if outcome.crash:
        return R.check(label, False, outcome.describe())
    return R.check(label, outcome.paid is None,
                   f"spent a credit on {outcome.paid}" if outcome.paid else "")


# --------------------------------------------------------- 1. the junk gate

JUNK = [
    ("", "empty"),
    ("  ", "whitespace only"),
    ("ab", "too short"),
    ("x" * 41, "over the 40-char cap"),
    ("1111", "one digit repeated"),
    ("aaaa", "one letter repeated"),
    ("aaaa1234", "keyboard-mashed letters"),
    ("'; DROP TABLE forklifts; --", "SQL injection"),
    ("<script>alert(1)</script>", "XSS payload"),
    ("../../etc/passwd", "path traversal"),
    ("${jndi:ldap://x/a}", "JNDI payload"),
    ("forklift \U0001f600 emoji", "emoji"),
    ("Доосан L7", "non-latin script"),
    ("%00null", "null byte escape"),
    ("a\tb\nc", "control characters"),
    ("!!!!!!", "punctuation only"),
    ("SELECT * FROM forklifts", "bare SQL"),
    ("hyster " + "9" * 60, "OEM plus a 60-digit run"),
]


def test_junk_gate(db) -> None:
    R.start("1. Junk gate -- garbage must never reach a paid call")
    for query, why in JUNK:
        for mode in ("", "model", "serial"):
            label = f"{why!r} mode={mode or 'auto'}"
            outcome = run_lookup(db, query, mode=mode)
            if not must_be_free(label, outcome):
                continue
            r = outcome.result
            R.check(f"{label} is refused", not r.found and not r.offer_web_search,
                    f"expected a refusal, got {outcome.describe()}")

    # Clicking "search the web" must not bypass the gate either.
    for query, why in JUNK:
        outcome = run_lookup(db, query, mode="", web=True)
        if must_be_free(f"web=1 on {why!r} stays free", outcome):
            R.check(f"web=1 on {why!r} is refused", not outcome.result.found,
                    outcome.describe())

    # The DB must survive the injection payloads.
    count = db.query(Forklift).count()
    R.check("the forklifts table survived the injection payloads", count > 0,
            f"row count is {count}")


# ------------------------------------------------- 2. OEM is required on both

NO_OEM = ["8FGCU25", "H50FT", "G004V01515D", "L7-00116", "FC 5200", "A269V01501P",
          "30D-9", "CPCD25-K2", "RM 6025"]


def test_oem_required(db) -> None:
    R.start("2. OEM requirement -- a bare model or serial is refused for free")
    for query in NO_OEM:
        for mode in ("model", "serial"):
            label = f"{query!r} mode={mode}"
            outcome = run_lookup(db, query, mode=mode)
            if not must_be_free(label, outcome):
                continue
            r = outcome.result
            R.check(f"{label} refused", not r.found, outcome.describe())
            R.check(f"{label} says why",
                    "manufacturer" in (r.message or "").lower(),
                    f"message does not mention the manufacturer: {r.message!r}")

    for alias, expected in [("toyota 8FGCU25", "Toyota"), ("daewoo L7-00116", "Bobcat/Doosan"),
                            ("cat EP16", "CAT/Mitsubishi"), ("hyster H50FT", "Hyster"),
                            ("YALE ERP040", "Yale"), ("  linde   H50D  ", "Linde"),
                            ("Club Car Carryall", "Club Car")]:
        oem, rest = split_oem(alias)
        R.check(f"split_oem({alias!r}) -> {expected}", oem == expected and bool(rest.strip()),
                f"got oem={oem!r} rest={rest!r}")


# ---------------------------------------------------- 3. serial decode edges

# expect: "decode" = offline decode, "refuse" = free refusal,
#         "web" = a paid serial-identify fallback is acceptable here
SERIAL_CASES = [
    ("Hyster A269V01501P", "decode", "known prefix, valid year letter"),
    ("Hyster A269", "decode", "bare 4-character prefix"),
    ("Hyster X999V01501P", "decode", "unknown prefix, still decodes structurally"),
    ("Doosan L7-00116", "decode", "known Doosan prefix"),
    ("Doosan BL-00003", "decode", "Doosan prefix with a year-table hit"),
    ("Yale 123456", "decode", "pre-1995 six-digit Yale"),
    ("Yale H1234", "decode", "pre-1995 alpha-prefixed Yale"),
    ("Hyster A269V01501I", "refuse", "I is not in the 23-letter year alphabet"),
    ("Hyster A269V01501O", "refuse", "O is not in the year alphabet"),
    ("Hyster A269V01501Q", "refuse", "Q is never used"),
    ("Hyster A269V0150P", "refuse", "4-digit sequence, one short"),
    ("Hyster A269V015011P", "refuse", "6-digit sequence, one long"),
    ("Doosan ZZ-99999", "web", "well-formed but unknown Doosan prefix"),
    ("Komatsu 12345678", "web", "no serial scheme on file for this OEM"),
]


def test_serial_edges(db) -> None:
    R.start("3. Serial decoding -- malformed, boundary and unknown prefixes")
    this_year = time.gmtime().tm_year
    for query, expect, note in SERIAL_CASES:
        label = f"{query!r} ({note})"
        outcome = run_lookup(db, query, mode="serial")
        if outcome.crash:
            R.fail(label, outcome.describe())
            continue

        if expect == "web":
            R.check(f"{label} -> web fallback is acceptable",
                    outcome.paid is not None or (outcome.result and outcome.result.found),
                    outcome.describe())
            continue

        if expect == "refuse":
            refused = (outcome.free and outcome.result is not None
                       and not outcome.result.found)
            R.check(f"{label} -> refused for free, no credit spent", refused,
                    f"a structurally impossible serial should be rejected with a format "
                    f"message, but it {outcome.describe()}")
            if refused:
                msg = (outcome.result.message or "").lower()
                R.check(f"{label} explains the format",
                        "11 characters" in msg and "year letter" in msg,
                        f"message does not state the format: {outcome.result.message!r}")
            continue

        # expect == "decode"
        if not must_be_free(f"{label} stays offline", outcome):
            continue
        r = outcome.result
        decoded = bool(r.serial and r.serial.kind not in (None, "unrecognized"))
        R.check(f"{label} decodes offline", decoded, outcome.describe())
        if decoded:
            future = [y for y in (r.serial.candidate_years or []) if y > this_year]
            R.check(f"{label} offers no future build year", not future, f"future: {future}")
            R.check(f"{label} has a plain-English summary", bool(r.serial.summary),
                    "summary is empty")

    # A confidently decoded serial must stay offline even when web=1 is forced.
    for query in ("Hyster A269V01501P", "Doosan L7-00116", "Hyster A269"):
        must_be_free(f"{query!r} stays offline with web=1",
                     run_lookup(db, query, mode="serial", web=True))


# -------------------------------------------------------- 4. normalisation

SPELLINGS = ["Hyster A269V01501P", "hyster a269v01501p", "HYSTER A269V01501P",
             "Hyster  a269-v01501-p", "hyster A269 V01501 P", "Hyster a269/v01501/p",
             "  hyster   A269V01501P  ", "Hyster a269.v01501.p", "hyster_A269V01501P"]


def test_normalisation(db) -> None:
    R.start("4. Normalisation -- one serial, many spellings, one answer")
    baseline = None
    for query in SPELLINGS:
        outcome = run_lookup(db, query, mode="serial")
        if not must_be_free(f"{query!r} stays offline", outcome):
            continue
        s = outcome.result.serial
        if not s:
            R.fail(f"{query!r}", f"did not decode: {outcome.describe()}")
            continue
        shape = (s.kind, s.prefix, s.plant, s.sequence, s.year_code,
                 tuple(s.candidate_years or ()), tuple(s.families or ()))
        if baseline is None:
            baseline, first = shape, query
            R.ok(f"{query!r} sets the baseline", f"{s.prefix} {s.plant} {s.sequence} {s.year_code}")
        else:
            R.check(f"{query!r} decodes identically to {first!r}", shape == baseline,
                    f"{shape}\n        != {baseline}")


# --------------------------------------------------------- 5. strategy routing


def test_routing(db) -> None:
    R.start("5. Routing -- the right strategy for the right shape of query")

    # A model query that misses the catalog must use the SPEC SHEET strategy.
    for query in ("Komatsu FG30T-16", "Linde H50D", "Hyundai 30D-9"):
        outcome = run_lookup(db, query, mode="model")
        R.check(f"{query!r} mode=model uses the spec-sheet search",
                outcome.paid == "tavily.search_spec_sheet",
                f"expected tavily.search_spec_sheet, got {outcome.describe()}")

    # An undecodable serial must use the SERIAL IDENTIFY strategy, not spec sheets.
    for query in ("Doosan ZZ-99999", "Komatsu 12345678"):
        outcome = run_lookup(db, query, mode="serial")
        R.check(f"{query!r} mode=serial uses the serial-identify search",
                outcome.paid in (None, "tavily.answer_search"),
                f"expected tavily.answer_search, got {outcome.describe()}")

    # mode=model must never emit a serial payload, mode=serial never a loose model match.
    outcome = run_lookup(db, "Hyster A269V01501P", mode="model")
    if outcome.result is not None:
        R.check("mode=model does not decode a serial", outcome.result.serial is None,
                f"serial payload leaked into the model tab: {outcome.result.serial}")
    else:
        R.ok("mode=model does not decode a serial", "went to the web instead, as expected")

    # A model name containing a brand word must not be mangled by OEM stripping.
    for query in ("Club Car Carryall 700", "club car Carryall"):
        outcome = run_lookup(db, query, mode="")
        R.check(f"{query!r} is handled as a model", outcome.crash is None, outcome.describe())

    # _looks_like_model picks the strategy in auto mode. The FC pair is the
    # interesting one: FC- is a real Doosan prefix AND Crown sells FC 45xx/52xx,
    # so only the separator tells the two apart once normalize() has run.
    for text, expected in [("8FGCU25", True), ("FC 5200", True), ("FC5200", True),
                           ("FC-5200", False), ("A269V01501P", False), ("L7-00116", False),
                           ("l7-00116", False), ("", False), ("30D-9", True),
                           ("G004V01515D", False), ("B463D01234N", False)]:
        got = L._looks_like_model(db, text)
        R.check(f"_looks_like_model({text!r}) is {expected}", got == expected, f"got {got}")

    # A decoder must not confidently claim another brand's serial.
    for query, wrong in [("A269V01501P", "Bobcat/Doosan"), ("L7-00116", "Hyster")]:
        cands = detect(db, query, manufacturer=wrong) or []
        strong = [c for c in cands if getattr(c, "confidence", 0) >= L.WEB_CONFIDENCE_FLOOR]
        R.check(f"{query!r} is not confidently claimed by {wrong}", not strong,
                f"claimed by: {[(c.decoder, c.confidence) for c in strong]}")


# ------------------------------------------------------------ 6. catalog hits


def test_catalog(db) -> None:
    R.start("6. Catalog hits -- a known model resolves offline, never via the web")
    rows = db.query(Forklift).filter(Forklift.model.isnot(None)).order_by(Forklift.id).all()
    sample = rows[:: max(len(rows) // 30, 1)][:30]
    R.check("sample is non-empty", bool(sample), "no forklifts in the catalog")
    for f in sample:
        query = f"{f.manufacturer} {f.model}"
        for mode in ("", "model"):
            label = f"{query!r} mode={mode or 'auto'}"
            outcome = run_lookup(db, query, mode=mode)
            if not must_be_free(label, outcome):
                continue
            r = outcome.result
            R.check(f"{label} found in the DB", r.found and r.already_in_db, outcome.describe())
            R.check(f"{label} returns the right record", r.existing_id == f.id,
                    f"expected #{f.id}, got #{r.existing_id}")


# ------------------------------------------------------- 7. regression guards


def test_regressions(db) -> None:
    R.start("7. Regression guards -- the three defects fixed on 2026-10-01")

    # (a) a series value must never carry the word "Series"
    for raw, expected in [("9 Series", "9"), ("8-Series", "8"), ("FC 5700 series", "FC 5700"),
                          ("Series", None), ("series", None), ("Fortis", "Fortis"),
                          ("E80-120XN", "E80-120XN"), ("ESC AD", "ESC AD"), ("3-Series.", "3"),
                          (None, None), ("", None), ("  G3  ", "G3")]:
        got = clean_series(raw)
        R.check(f"clean_series({raw!r}) -> {expected!r}", got == expected, f"got {got!r}")
    R.check("the ForkliftSpecs validator strips it too",
            ForkliftSpecs(series="9 Series").series == "9",
            f"got {ForkliftSpecs(series='9 Series').series!r}")
    for f in db.query(Forklift).filter(Forklift.series.ilike("%series%")).all():
        R.fail("a catalog row carries 'Series'",
               f"#{f.id} {f.manufacturer} {f.model}: series={f.series!r}")

    # (b) capacity must not be a pounds conversion for a metric-code OEM, and a
    #     pounds-code OEM must never be touched.
    for mfr, model, given, expected in [
        ("Hyundai", "30D-9", 2722.0, 3000.0), ("Doosan", "D35S-5", 3175.0, 3500.0),
        ("Heli", "CPCD25-K2", 2268.0, 2500.0), ("Hyundai", "110D-9", 10886.0, 11000.0),
        ("Linde", "H50D", 5000.0, 5000.0), ("Komatsu", "FG30T-16", 3000.0, 3000.0),
        ("Toyota", "8FGCU25", 2268.0, 2268.0), ("Hyster", "H50FT", 2268.0, 2268.0),
        ("Crown", "FC 5200", 1814.0, 1814.0), ("Yale", "ERP040", 1814.0, 1814.0),
        ("Jungheinrich", "EFG 320", 2000.0, 2000.0), ("EP", "EFL181", 1800.0, 1800.0),
        ("Linde", "H50D", 1800.0, 1800.0), ("Hyundai", "30D-9", None, None),
        ("Hyundai", "", 2722.0, 2722.0), ("", "30D-9", 2722.0, 2722.0),
        ("Heli", "CPD25-GE2G", 2268.0, 2500.0),
    ]:
        got, _ = reconcile_capacity(mfr, model, given)
        R.check(f"reconcile_capacity({mfr!r}, {model!r}, {given}) -> {expected}",
                got == expected, f"got {got}")

    # (c) PDF provenance, including domain-lookalike spoofing
    for url, domains, expected in [
        ("https://www.linde-mh.com/a.pdf", ["linde-mh.com"], True),
        ("https://linde-mh.com/a.pdf", ["linde-mh.com"], True),
        ("https://docs.linde-mh.com/a.pdf", ["linde-mh.com"], True),
        ("https://linde-mh.com.evil.example/a.pdf", ["linde-mh.com"], False),
        ("https://notlinde-mh.com/a.pdf", ["linde-mh.com"], False),
        ("https://evil.example/?x=linde-mh.com", ["linde-mh.com"], False),
        ("https://gruasyequiposgarcia.com/LINDE-H70D.pdf", ["linde-mh.com"], False),
        ("https://linde-mh.com/a.pdf", None, False),
        ("", ["linde-mh.com"], False),
        ("not a url at all", ["linde-mh.com"], False),
    ]:
        got = web_identify._on_oem_domain(url, domains)
        R.check(f"_on_oem_domain({url!r}) is {expected}", got == expected, f"got {got}")

    missing = sorted({m for (m,) in db.query(Forklift.manufacturer).distinct()
                      if m not in OEM_DOMAINS})
    R.check("every catalog manufacturer has an OEM domain on file", not missing,
            f"no domain for: {missing}")

    # A third-party PDF is kept as a fallback but must not halt escalation.
    saved_verify, saved_urls = web_identify.verify_pdf_url, tavily_client.pdf_urls
    web_identify.verify_pdf_url = lambda url: 200
    tavily_client.pdf_urls = lambda s: s["results"]
    try:
        finder = web_identify._PdfFinder("Linde H50D", "H50D", ["linde-mh.com"])
        third = {"results": [{"url": "https://dealer.example/LINDE-H70D-15000-LB.pdf",
                              "title": "Linde H70D", "raw_content": "H50D H60D H70D H80D"}]}
        own = {"results": [{"url": "https://www.linde-mh.com/ds_h50_h80.pdf",
                            "title": "Linde H50-H80", "raw_content": "H50D H60D H70D H80D"}]}
        finder.consider(third, None)
        R.check("a third-party PDF is kept as a fallback", bool(finder.best()), "nothing kept")
        R.check("a third-party PDF does not halt escalation", not finder.have_oem_model_pdf,
                "escalation would have stopped on a third-party hit")
        finder.consider(own, None)
        best = finder.best()
        R.check("the OEM's own sheet then wins",
                bool(best) and best[0].startswith("https://www.linde-mh.com") and best[3],
                f"best={best}")
        # Same two results in the other order: the OEM must still win outright.
        f2 = web_identify._PdfFinder("Linde H50D", "H50D", ["linde-mh.com"])
        f2.consider({"results": third["results"] + own["results"]}, None)
        R.check("the OEM's sheet wins even when ranked below a third party",
                bool(f2.best()) and f2.best()[3], f"best={f2.best()}")
    finally:
        web_identify.verify_pdf_url = saved_verify
        tavily_client.pdf_urls = saved_urls


# ----------------------------------------------------------- 8. concurrency


def test_concurrency() -> None:
    R.start("8. Concurrency -- 200 parallel lookups across 20 threads")
    queries = ([q for q, _, _ in SERIAL_CASES] + SPELLINGS + NO_OEM
               + [q for q, _ in JUNK] + ["Hyster A269", "Doosan L7-00116"])
    work = (queries * 10)[:200]
    errors: list[str] = []
    done = 0
    lock = threading.Lock()
    mark_before = len(_spend_log)

    def one(query: str) -> None:
        nonlocal done
        db = SessionLocal()
        try:
            L.lookup(db, query, web=False, mode="serial")
            with lock:
                done += 1
        except PaidCall:
            with lock:
                done += 1  # a blocked web fallback is a normal outcome here
        except Exception:  # noqa: BLE001 -- the point is to catch anything
            with lock:
                errors.append(f"{query!r}: {traceback.format_exc(limit=2)}")
        finally:
            db.close()

    started = time.time()
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(one, work))
    elapsed = time.time() - started

    R.check(f"all {len(work)} parallel lookups completed", done == len(work),
            f"{done}/{len(work)} completed")
    R.check("no exceptions under concurrency", not errors,
            f"{len(errors)} error(s); first: {errors[0] if errors else ''}")
    print(f"  {len(work)} lookups in {elapsed:.1f}s "
          f"({len(work) / max(elapsed, 0.001):.0f}/s, "
          f"{len(_spend_log) - mark_before} blocked web fallbacks)")

    db = SessionLocal()
    try:
        dupes = db.execute(sql_text(
            "SELECT internal_serial, COUNT(*) c FROM forklifts "
            "WHERE internal_serial IS NOT NULL GROUP BY 1 HAVING c > 1")).fetchall()
        R.check("no duplicate SSC serial numbers", not dupes, f"duplicates: {dupes[:5]}")
        R.check("the catalog is intact", db.query(Forklift).count() > 0, "catalog is empty")
    finally:
        db.close()


# --------------------------------------------------------- 9. live OEM domains


def test_online() -> None:
    R.start("9. Live domains -- every OEM domain still resolves (--online)")
    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (compatible; forklift-db stress test)"}
    with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
        for oem, domains in sorted(OEM_DOMAINS.items()):
            for domain in domains:
                # Some apexes refuse TLS while www serves fine (unicarriers.com).
                # Either is enough: _on_oem_domain matches subdomains anyway.
                problems = []
                for host in (domain, f"www.{domain}"):
                    try:
                        r = client.get(f"https://{host}")
                        # 403 is a bot block, not a dead domain; 404 means the
                        # host answers but the site has moved.
                        if r.status_code < 500 and r.status_code != 404:
                            R.ok(f"{oem} -> {domain}", f"{host} HTTP {r.status_code}")
                            break
                        problems.append(f"{host} HTTP {r.status_code}")
                    except Exception as exc:  # noqa: BLE001
                        problems.append(f"{host} {type(exc).__name__}")
                else:
                    R.fail(f"{oem} -> {domain}", "; ".join(problems))


# ---------------------------------------------------------------------- main


def main() -> int:
    init_db()
    block_paid_calls()
    print("Tavily and Anthropic calls are blocked: this suite cannot spend money.")

    db = SessionLocal()
    try:
        test_junk_gate(db)
        test_oem_required(db)
        test_serial_edges(db)
        test_normalisation(db)
        test_routing(db)
        test_catalog(db)
        test_regressions(db)
    finally:
        db.close()

    test_concurrency()
    if ONLINE:
        test_online()
    else:
        print("\n(section 9 skipped -- pass --online to check the OEM domains over HTTP)")

    return R.summary()


if __name__ == "__main__":
    raise SystemExit(main())
