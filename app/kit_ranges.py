"""Read the model ranges a kit documents in its own `notes` field.

A kit row's notes look like::

    OEM: Toyota
    Fits: 8FGCU20-32, 8FGCU35U-70U, 8FGU20-32, 8FG35U-80U, 8FDU20-32
    Caveats: ...

The `Fits:` line is SSC's own fitment documentation, and this module exists to
read it literally -- nothing more. A truck that falls inside a stated range is
documented to fit; a truck outside every stated range is a question only a
technician can answer, so this module must never widen a range, guess at a
token it cannot read, or carry a code across OEMs.

Three rules follow from that, and all three are enforced here rather than left
to the caller:

1. **Every designation is scoped to the kit's declared OEM** (the `OEM:` line).
   Model codes collide across brands -- Yale `GP40N` and CAT `GP40N` are
   different trucks -- and CLAUDE.md's standing rule that kits never transfer
   between Hyster/Yale twins is the same hazard. A `Fits:` line that names a
   brand other than the declared OEM (Toyota kit 42's "Raymond 730") yields a
   `foreign` entry, never a designation.
2. **A suffix can contain digits.** `GC35-50K5` is GC35K5..GC50K5, not
   GC35..GC50 with a stray `K5`, and `EC15-18LN3` is EC15LN3..EC18LN3. A
   `[A-Za-z]*` suffix pattern silently drops these, so the decomposition here
   tries every digit run in the token and lets the range's own consistency
   (shared prefix, shared suffix, lo < hi, matching digit width) pick the
   reading.
3. **Anything unreadable is reported, never guessed past.** The notes also hold
   prose ("MIT", "(serial # 10231+)", "Platinum II trucks"), dates, SKUs and
   mojibake. Those land in `unparsed` / `ignored` / `series_refs` so a person
   can look at them; they are never silently dropped and never linked.

`Caveats:` is deliberately not parsed. It carries serial-number cutoffs, dates
and installer notes, and sometimes a counterpart brand -- none of it is the
fitment statement.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.ai.oem_alias import ALIASES

# Brand word -> canonical catalog manufacturer, longest phrase first so
# "big joe" beats "big" and "stewart & stevenson" beats "stewart".
# "mit" is Mitsubishi's own shorthand in these notes, not a separate OEM.
_BRAND_WORDS: list[tuple[str, str]] = sorted(
    [(alias, oem) for oem, aliases in ALIASES.items() for alias in aliases]
    + [("mit", "CAT/Mitsubishi")],
    key=lambda pair: -len(pair[0]),
)

# Token separators. A BARE `/` is not one: it distributes a shared prefix or
# suffix (`GC/GLC/GDC135-160VX`, `J30-40XN/XNT`). A SPACED ` / ` is -- kit 2
# uses it to start a second sentence ("GC35-50K and GP40-50K / Mitsubishi
# Forklift FGC35-50K"), and distribution slashes are never spaced.
_SPLIT = re.compile(r",|\||\band\b|&| / ", re.I)

# `thru` joins the two ends of one range rather than separating two models:
# "G20-N7 thru G33-N7" is a range, so it becomes the spaced dash below.
_THRU = re.compile(r"\s+(?:thru|through)\s+", re.I)

# Prose that leads a model code without being part of it.
_LEAD_NOISE = ("forklift", "forklifts", "truck", "trucks", "model", "models")

# Two or more lower-case letters in a row is a word, not a model code. One on
# its own is a typo inside a code and must survive: kit 34 writes `7FGu35-80`.
_LOWER_WORD = re.compile(r"[a-z]{2}")

# All-caps words that are prose rather than a model code.
_PROSE = {
    "SERIES", "TRUCK", "TRUCKS", "MODELS", "MODEL", "ALL", "NEW", "AKA",
    "CAN", "NON", "OEM", "FITS", "ENG", "ONLY", "PIN", "SER", "OLD", "STYLE",
}

# A kit SKU quoted inside a Fits: line (kit 45 cites 174-R750-00G05).
_SKU = re.compile(r"^174-", re.I)

# Prefixes that identify the OTHER half of the Hyster/Yale twin pair, from
# CLAUDE.md's own tire table. The Hyster kits write the Hyster model and its
# Yale counterpart on one line -- kit 77 is
# "H135-160FT | S135-160FT | GC/GLC/GDC135-160VX | GP/GLP/GDP135-160VX" --
# while declaring `OEM: Hyster`. Reading the second half as Hyster would both
# invent Hyster-branded rows for Yale trucks and put a Hyster/Yale link one
# join away, which CLAUDE.md forbids outright. So they are set aside as
# another brand's codes, the same as kit 42's "Raymond 730".
#
# Only this direction is encoded. Yale's Hyster counterparts would be `H` `S`
# `E` `J`, single letters that collide with Yale's own `ERC`/`ERP`/`ESC`, and
# no Yale kit in the table names a Hyster truck -- so guessing at that
# direction would cost accuracy and buy nothing.
_TWIN_PREFIXES: dict[str, tuple[str, str, ...]] = {
    "Hyster": ("Yale", "ERC", "ERP", "ESC", "GDC", "GDP", "GLC", "GLP", "GC", "GP"),
}

_SERIES_WORD = re.compile(r"\bseries\b", re.I)
_PAREN = re.compile(r"\([^)]*\)|\([^)]*$")
_DIGITS = re.compile(r"\d+")


# --------------------------------------------------------------- data classes


@dataclass(frozen=True)
class ModelRange:
    """`GC35-50K5` -> prefix GC, suffix K5, 35..50, written 2 digits wide."""

    oem: str
    prefix: str
    suffix: str
    lo: int
    hi: int
    width: int
    source: str

    def __str__(self) -> str:
        return self.render(self.lo) + "-" + self.render(self.hi)

    def render(self, number: int) -> str:
        """`ERC030-040VAN` members keep the written zero padding."""
        body = str(number)
        if len(body) < self.width:
            body = body.zfill(self.width)
        return self.prefix + body + self.suffix

    def contains(self, model: str) -> bool:
        """True when `model` is a designation this range names outright.

        Punctuation-insensitive on the prefix and suffix, because the notes and
        the catalog punctuate the same truck differently -- kit 69 writes
        `G20-N7`, the catalog row is `G20N-7`. The OEM, the number and the
        letters all still have to agree exactly; only the dashes are forgiven.
        """
        for prefix, number, suffix, _width in _decompose(normalise(model)):
            if _bare(prefix) == _bare(self.prefix) and _bare(suffix) == _bare(self.suffix):
                if self.lo <= number <= self.hi:
                    return True
        return False

    def members(self, vocabulary: set[int]) -> list[str]:
        """Designations the range names, over a supplied numbering vocabulary.

        The endpoints are stated by the kit, so they are always members. The
        interior cannot be enumerated by counting -- `8FGCU20-32` is 20/25/30/32,
        not thirteen trucks -- so the caller supplies the capacity numbers that
        OEM actually uses, observed from the catalog. Every interior candidate
        is still only a candidate: it has to be confirmed against the OEM before
        it becomes a row.
        """
        numbers = {self.lo, self.hi}
        numbers |= {n for n in vocabulary if self.lo < n < self.hi}
        return [self.render(n) for n in sorted(numbers)]


@dataclass
class KitFits:
    """Everything one kit's `Fits:` line says, sorted by how certain it is."""

    kit_id: int | None = None
    sku: str = ""
    oem: str | None = None
    exact: list[str] = field(default_factory=list)
    ranges: list[ModelRange] = field(default_factory=list)
    series_refs: list[str] = field(default_factory=list)
    foreign: list[tuple[str, str]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)

    def designations(self, vocabulary: set[int] | None = None) -> list[str]:
        """Exact codes plus every range member, de-duplicated, order kept."""
        out: list[str] = []
        seen: set[str] = set()
        for name in self.exact:
            if name not in seen:
                seen.add(name)
                out.append(name)
        for rng in self.ranges:
            for name in rng.members(vocabulary or set()):
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        return out

    @property
    def readable(self) -> bool:
        return bool(self.oem) and bool(self.exact or self.ranges)


# -------------------------------------------------------------- normalisation


def normalise(text: str) -> str:
    """Upper-case and close internal gaps: `7 FGCSU20` -> `7FGCSU20`.

    Crown writes the same truck as `SP 3500` and `SP3500` in different rows, so
    comparison has to ignore the space. Hyphens are kept -- `B20T-7` and
    `B20T7` are not interchangeable the way the spaces are.
    """
    return re.sub(r"\s+", "", text).upper()


def _bare(text: str) -> str:
    """Drop the dashes too, for comparing one spelling of a truck to another."""
    return text.replace("-", "").replace(".", "")


def _clean(text: str) -> str:
    """Drop mojibake and control characters, leave the shape alone."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(
        ch for ch in text
        if ch == "\n" or (ch.isprintable() and ord(ch) < 128)
    )


def _label(notes: str, label: str) -> str | None:
    for line in notes.splitlines():
        head, sep, rest = line.partition(":")
        if sep and head.strip().lower() == label:
            return rest.strip()
    return None


# ----------------------------------------------------------- token decomposing


def _decompose(token: str) -> list[tuple[str, int, str, int]]:
    """Every (prefix, number, suffix, written width) split of a token.

    One split per digit run, because which run carries the capacity cannot be
    told from the string alone: in `GC50K5` it is the `50`, in `70K5` (the far
    side of `GC60K5-70K5`) it is the `70`, and the two look identical in
    isolation. The range's own consistency decides.
    """
    out = []
    for m in _DIGITS.finditer(token):
        body = m.group(0)
        out.append((token[: m.start()], int(body), token[m.end():], len(body)))
    return out


def _widths_agree(lo_width: int, hi_width: int) -> bool:
    """Endpoints are written the same width, or the high end gains one digit.

    Gaining a digit is ordinary: Hyster `H80-120FT` is H80FT..H120FT and CAT
    `P8000-12000` is P8000..P12000. What the rule has to exclude is a
    SINGLE-digit low end, because that is never a capacity code -- it is the
    tail of a suffix the split guessed wrong. Without the `lo_width >= 2`
    floor, `FB16PNT2-20` reads as FB16PNT2..FB16PNT20 (splitting at the `2` of
    `PNT2`) instead of FB16PNT2..FB20PNT2.
    """
    if lo_width == hi_width:
        return True
    return hi_width == lo_width + 1 and lo_width >= 2


def _pair(lo_token: str, hi_token: str) -> tuple[str, str, int, int, int] | None:
    """Read `lo_token`-`hi_token` as one range, or return None.

    Scored rather than pattern-matched: every decomposition of each side is
    tried, impossible pairings are rejected outright, and the best-scoring
    survivor wins only if it is the unique best. A tie means the token is
    genuinely ambiguous and belongs in `unparsed`.
    """
    best: tuple[tuple[int, int], tuple[str, str, int, int, int]] | None = None
    ties = 0
    for lo_prefix, lo, lo_suffix, lo_width in _decompose(lo_token):
        for hi_prefix, hi, hi_suffix, hi_width in _decompose(hi_token):
            # The high side may inherit the prefix (`GC35-50K5`) or repeat it
            # (`8FBEU15-8FBEU20`), but a prefix written only on the high side
            # is not a range -- that shape is `425-C4`, one model.
            if hi_prefix and hi_prefix != lo_prefix:
                continue
            # Two different non-empty suffixes are two different trucks:
            # `EC22N2-EC30LN2` is not a range this module may read.
            if lo_suffix and hi_suffix and lo_suffix != hi_suffix:
                continue
            if lo >= hi or not _widths_agree(lo_width, hi_width):
                continue
            suffix = lo_suffix or hi_suffix
            # Hyundai leads with the capacity (`15-23BRP-9` is 15BRP-9..23BRP-9),
            # so an empty prefix is legal -- but only when a suffix carries the
            # model letters. Two bare numbers with nothing else are a date or a
            # voltage, never a range.
            if not lo_prefix and not suffix:
                continue
            candidate = (lo_prefix, suffix, lo, hi, lo_width)
            score = (
                1 if lo_suffix == hi_suffix else 0,   # no suffix inherited
                1 if hi_prefix == lo_prefix else 0,   # no prefix inherited
            )
            if best is None or score > best[0]:
                best, ties = (score, candidate), 1
            elif score == best[0] and candidate != best[1]:
                ties += 1
    if best is None or ties > 1:
        return None
    return best[1]


def _as_range(oem: str, token: str) -> ModelRange | None:
    """Try a token as a range. Every dash is tried as the separator.

    A spaced dash is the author separating two whole endpoints
    (`B15T-7P - B20T-7P`), so it collapses to a plain one. After that the
    separator is not knowable from the shape -- `BC20-32S-7` carries three
    dashes and means BC20S-7..BC32S-7 -- so each is tried and a reading wins
    only if it is the only one that works.
    """
    if " - " in token:
        head, _, tail = token.partition(" - ")
        token = head.strip() + "-" + tail.strip()
    # An endpoint may not reach across a space. `E45-65Z G108` is Hyster
    # E45Z..E65Z with a series number after it, and closing that gap first
    # would read the series number into the suffix as E45ZG108..E65ZG108.
    if re.search(r"\s", token):
        return None

    flat = normalise(token)
    hits: list[tuple[str, str, int, int, int]] = []
    for m in re.finditer(r"-", flat):
        lo_token, hi_token = flat[: m.start()], flat[m.end():]
        if not _has_letter(lo_token) and not _has_letter(hi_token):
            continue
        found = _pair(lo_token, hi_token)
        if found and found not in hits:
            hits.append(found)
    if len(hits) != 1:
        return None
    return _build(oem, token, hits[0])


def _build(oem: str, source: str, found: tuple[str, str, int, int, int]) -> ModelRange:
    prefix, suffix, lo, hi, width = found
    return ModelRange(oem, prefix, suffix, lo, hi, width, source)


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _is_code_shaped(token: str) -> bool:
    """A gate both designations and ranges must pass before being read.

    Keeps prose out of the range reader. Without it `CarryAll 300-500` reads as
    a range from CARRYALL300 to CARRYALL500, and `FC5700 - 4 wheel Sit-down`
    tries to range to a sentence.
    """
    token = token.strip()
    if not token or not _has_letter(token) or _LOWER_WORD.search(token):
        return False
    if not re.fullmatch(r"[A-Z0-9][A-Za-z0-9.\- ]*", token):
        return False
    return not any(word in _PROSE for word in re.split(r"[\s\-]+", token))


def _is_designation(token: str) -> bool:
    """A single model code, as opposed to a range or a phrase."""
    return _is_code_shaped(token) and len(normalise(token)) >= 2


def _split_slash(token: str) -> list[str] | None:
    """Slash forms that name several trucks at once.

    `GC/GLC/GDC135-160VX` shares a body across prefixes and `J30-40XN/XNT`
    shares a body across suffixes; `E35SH/E35PHL` is simply two whole codes.
    Any other use of `/` is prose and is left to be reported.
    """
    if "/" not in token:
        return None
    flat = normalise(token)
    head = re.fullmatch(r"([A-Z]+(?:/[A-Z]+)+)(\d.*)", flat)
    if head:
        return [p + head.group(2) for p in head.group(1).split("/")]
    tail = re.fullmatch(r"(.*?\d[A-Z0-9.\-]*?)([A-Z]+(?:/[A-Z]+)+)", flat)
    if tail:
        return [tail.group(1) + s for s in tail.group(2).split("/")]
    parts = flat.split("/")
    if len(parts) > 1 and all(_is_designation(p) for p in parts):
        return parts
    return None


# -------------------------------------------------------------------- parsing


def parse_notes(notes: str | None, kit_id: int | None = None, sku: str = "") -> KitFits:
    """Read one kit's notes into designations, ranges and everything unread."""
    result = KitFits(kit_id=kit_id, sku=sku)
    if not notes:
        result.unparsed.append("(no notes)")
        return result

    text = _clean(notes)
    oem_line = _label(text, "oem")
    fits_line = _label(text, "fits")
    if not oem_line:
        result.unparsed.append("(no OEM: line)")
        return result
    result.oem = oem_line.strip()
    if not fits_line:
        result.unparsed.append("(no Fits: line)")
        return result

    # Parentheticals are caveats, not fitment ("(serial # 10231+)", "(elec)").
    # They are reported, and they become separators: kit 45 relies on that to
    # keep `9BDRU13-15` apart from the `Raymond ...` that follows its bracket.
    kept = _PAREN.findall(fits_line)
    body = _THRU.sub(" - ", _PAREN.sub(" , ", fits_line))
    result.ignored.extend(p.strip() for p in kept if p.strip())

    for raw in _SPLIT.split(body):
        token = raw.strip(" .,;-")
        if token:
            _classify(result, token)
    _dedupe(result)
    return result


def _dedupe(result: KitFits) -> None:
    """The notes repeat themselves -- kit 7 names GP40-55N1 twice."""
    result.exact = list(dict.fromkeys(result.exact))
    result.series_refs = list(dict.fromkeys(result.series_refs))
    seen: set[tuple[str, str, int, int]] = set()
    kept = []
    for rng in result.ranges:
        key = (rng.prefix, rng.suffix, rng.lo, rng.hi)
        if key not in seen:
            seen.add(key)
            kept.append(rng)
    result.ranges = kept


def _classify(result: KitFits, token: str) -> None:
    """Sort one token into exactly one bucket of `result`."""
    oem = result.oem or ""

    if _SKU.match(token):
        result.ignored.append(token)
        return

    # A leading brand word either names this kit's OEM (strip it and go on) or
    # names a different one, which is never linked.
    lowered = token.lower()
    for alias, canonical in _BRAND_WORDS:
        if lowered == alias or lowered.startswith(alias + " "):
            if canonical != oem:
                result.foreign.append((canonical, token))
                return
            token = token[len(alias):].strip()
            if not token:
                return
            break

    lowered = token.lower()
    for noise in _LEAD_NOISE:
        if lowered.startswith(noise + " "):
            token = token[len(noise):].strip()
            break

    if _SERIES_WORD.search(token):
        designation = _SERIES_WORD.sub(" ", token).strip(" -.")
        for part in (designation, *_segments(designation)):
            if _is_designation(part):
                result.series_refs.append(normalise(part))
                return
        result.unparsed.append(token)
        return

    parts = _split_slash(token)
    if parts:
        for part in parts:
            _accept(result, part, token)
        return

    token = _glue(token)

    if not re.search(r"\s", token):
        if not _accept(result, token, token):
            result.unparsed.append(token)
        return

    # A spaced dash joins the two ends of one range (`DP100N - DP160N`,
    # `B15T-7P - B20T-7P`), so that shape gets read whole before anything else
    # -- but only as a range. `H30T - 1204` is a model and a series number, and
    # reading it whole would hand back the invented model `H30T-1204`.
    if " - " in token and _accept(result, token, token, range_only=True):
        return

    # Otherwise the notes are hanging something extra off a code with a space:
    # a series number (`E45-65Z G108`, `H30T 1202`), a year, or a whole
    # sentence (`FC5700 - 4 wheel Sit-down`). Read the outer pieces on their
    # own and report what is left over. Such a piece has to carry a number of
    # its own -- `ETR` out of `ETR 230-335` is a prefix, not a truck.
    for part in _segments(token):
        if any(ch.isdigit() for ch in part) and _accept(result, part, part):
            rest = token.replace(part, " ", 1).strip(" -.,")
            if rest:
                result.ignored.append(rest)
            return

    # Last try: the same token with its gaps closed, which is how `ETR 230-335`
    # becomes ETR230..ETR335. The shape is checked before the gaps close, since
    # closing them also upper-cases away the evidence that this is prose.
    if _is_code_shaped(token) and _accept(result, normalise(token), token):
        return
    result.unparsed.append(token)


def _glue(token: str) -> str:
    """Close a gap that splits one code: `7 FGCSU20` -> `7FGCSU20`.

    Only where the piece before the gap has no letter of its own, so it cannot
    be a designation and the join is the only reading left. `H30T 1202` keeps
    its gap -- `H30T` stands alone, and `1202` is Linde's series number. A
    date (`3-20-26 - KLM added ...`) or a model year (`2012 YDREX2`) has no
    letter either but is not half of a code, so neither is joined.
    """
    parts = re.split(r"\s+", token.strip())
    out: list[str] = []
    for part in parts:
        if out and not _has_letter(out[-1]) and not _is_date_like(out[-1]):
            out[-1] += part
        else:
            out.append(part)
    return " ".join(out)


def _is_date_like(text: str) -> bool:
    if "-" in text or "/" in text:
        return True
    return bool(re.fullmatch(r"(19|20)\d\d", text))


def _segments(token: str) -> list[str]:
    """The leading and trailing whitespace-separated pieces of a token.

    Tried in that order, so `E45-65Z G108` yields the range from the front and
    `Rail King RK330` the model from the end. Nothing in between is tried: a
    piece with prose on both sides of it is not something to read confidently.
    """
    parts = re.split(r"\s+", token.strip())
    if len(parts) < 2:
        return []
    out = [parts[0], parts[-1]]
    if " - " in token:
        head, _, tail = token.partition(" - ")
        out = [head.strip(), tail.strip(), *out]
    return [p for p in dict.fromkeys(out) if p]


def _accept(result: KitFits, token: str, source: str, range_only: bool = False) -> bool:
    """File `token` as a range or a designation. False if it read as neither."""
    token = token.strip(" .,;-")
    if not _is_code_shaped(token):
        return False
    twin = _twin_of(result.oem or "", token)
    if twin:
        result.foreign.append((twin, token))
        return True
    rng = _as_range(result.oem or "", token)
    if rng is not None:
        result.ranges.append(ModelRange(
            rng.oem, rng.prefix, rng.suffix, rng.lo, rng.hi, rng.width, source,
        ))
        return True
    if range_only or _looks_like_range(token):
        return False
    if _is_designation(token):
        result.exact.append(normalise(token))
        return True
    return False


def _twin_of(oem: str, token: str) -> str | None:
    """The other brand, when this code is the twin's and not the kit OEM's."""
    entry = _TWIN_PREFIXES.get(oem)
    if not entry:
        return None
    other, *prefixes = entry
    flat = normalise(token)
    return other if flat.startswith(tuple(prefixes)) else None


def _looks_like_range(token: str) -> bool:
    """A range this module refused to read -- and must not call a model either.

    `EC22N2-EC30LN2` and `E30HSDS3-E40HSD3` name two trucks each, with suffixes
    too far apart to range over; filing either as a single designation would
    invent a truck. The test is both ends carrying a letter and a digit under
    the same letter prefix, which is what separates them from the real
    dash-bearing model codes: `D50C-9`, `425-C4`, `SR1-HPT35`, `C5-1000`.
    """
    lo_token, sep, hi_token = normalise(token).partition("-")
    if not sep or not hi_token:
        return False

    def letters(text: str) -> str:
        return "".join(re.findall(r"^[A-Z]+", text))

    full = [t for t in (lo_token, hi_token) if _has_letter(t) and any(c.isdigit() for c in t)]
    return len(full) == 2 and letters(lo_token) == letters(hi_token) != ""


def parse_all(rows) -> list[KitFits]:
    """`rows` is any iterable of (kit_id, sku, notes)."""
    return [parse_notes(notes, kit_id, sku or "") for kit_id, sku, notes in rows]
