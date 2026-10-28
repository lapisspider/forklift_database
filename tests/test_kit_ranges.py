"""Checks for `app/kit_ranges.py`, the reader for a kit's `Fits:` line.

Run on its own:       .venv\\Scripts\\python.exe tests\\test_kit_ranges.py
or as section 9 of:   .venv\\Scripts\\python.exe tests\\stress_lookup.py

Free, offline and database-free -- it is pure string work on literal notes
copied out of the `kits` table.

What the cases are defending, in order of how much a mistake would cost:

  1. OEM scope. A `Fits:` line that names another brand must never produce a
     designation. A first pass at this linked Yale `GP40N` to a CAT kit because
     the codes look alike, which is CLAUDE.md's Hyster/Yale rule in a new
     costume: wrong fitment reaches a technician under a torque wrench.
  2. Suffixes with digits. `GC35-50K5` is GC35K5..GC50K5. A `[A-Za-z]*` suffix
     pattern reads it as GC35..GC50 and silently drops the K5 generation.
  3. Nothing invented. A token the module cannot read has to turn up in
     `unparsed`; a range it refuses must not come back as a model instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.kit_ranges import ModelRange, parse_notes  # noqa: E402


def notes(oem: str, fits: str, caveats: str | None = None) -> str:
    out = f"OEM: {oem}\nFits: {fits}"
    return out + (f"\nCaveats: {caveats}" if caveats else "")


# Every case: (label, OEM, Fits: text, expected ranges, expected exact models).
# A `None` expectation means "do not check this bucket here".
RANGE_CASES: list[tuple[str, str, str, list[str], list[str]]] = [
    # ---- the five written forms the notes actually use -------------------
    ("bare model", "Toyota", "8FBC15U", [], ["8FBC15U"]),
    ("numeric range, shared prefix", "Toyota", "8FGCU20-32",
     ["8FGCU20-8FGCU32"], []),
    ("range with a letter suffix", "Toyota", "8FGCU35U-70U",
     ["8FGCU35U-8FGCU70U"], []),
    ("suffix carrying digits", "CAT/Mitsubishi", "GC35-50K5",
     ["GC35K5-GC50K5"], []),
    ("suffix with digits on both ends", "CAT/Mitsubishi", "GC60K5-70K5",
     ["GC60K5-GC70K5"], []),
    ("both ends written out", "Toyota", "8FBEU15-8FBEU20",
     ["8FBEU15-8FBEU20"], []),

    # ---- the trap the suffix pattern sets -------------------------------
    # EC15LN3..EC18LN3, not EC15..EC18: the generation digit is inside the
    # suffix, and dropping it would claim an N3 kit fits an LN3 truck.
    ("three-character suffix", "CAT/Mitsubishi", "EC15-18LN3",
     ["EC15LN3-EC18LN3"], []),
    ("suffix ending in a digit", "CAT/Mitsubishi", "FB16PNT2-20",
     ["FB16PNT2-FB20PNT2"], []),

    # ---- widths, which is where a plausible range turns into a wrong one --
    ("high end gains a digit", "Hyster", "H80-120FT", ["H80FT-H120FT"], []),
    ("five-digit pound codes", "CAT/Mitsubishi", "P8000-12000",
     ["P8000-P12000"], []),
    ("zero padding is kept", "Yale", "ERC030-040VAN",
     ["ERC030VAN-ERC040VAN"], []),

    # ---- shapes that look like ranges and are not -----------------------
    ("config suffix is not a range", "Bobcat/Doosan", "D50C-9", [], ["D50C-9"]),
    ("series code is not a range", "Bobcat/Doosan", "B20T-7", [], ["B20T-7"]),
    ("prefix on the high end only", "Raymond", "Raymond 425-C4", [], ["425-C4"]),
    ("four digits against one", "Crown", "Crown C5-1000", [], ["C5-1000"]),
    ("high end below the low end", "Big Joe/EP Equipment", "EFLA251S-189",
     [], ["EFLA251S-189"]),

    # ---- dashes in the middle of the code -------------------------------
    ("suffix after the range", "Bobcat/Doosan", "BC20-32S-7",
     ["BC20S-7-BC32S-7"], []),
    ("capacity leads the code", "Hyundai", "15-23BRP-9",
     ["15BRP-9-23BRP-9"], []),
    ("spaced dash joins two ends", "Bobcat/Doosan", "B15T-7P - B20T-7P",
     ["B15T-7P-B20T-7P"], []),
    ("thru joins two ends", "Bobcat/Doosan", "Doosan G20-N7 thru G33-N7",
     ["G20-N7-G33-N7"], []),

    # ---- slash forms ----------------------------------------------------
    ("slash shares a body across prefixes", "Yale", "GC/GLC/GDC080-120VX",
     ["GC080VX-GC120VX", "GLC080VX-GLC120VX", "GDC080VX-GDC120VX"], []),
    ("slash shares a body across suffixes", "Hyster", "J30-40XN/XNT",
     ["J30XN-J40XN", "J30XNT-J40XNT"], []),
    ("slash between two whole codes", "Linde", "E35SH/E35PHL",
     [], ["E35SH", "E35PHL"]),

    # ---- spacing --------------------------------------------------------
    # A gap splitting one code closes (`7 FGCSU20`); a gap before a series
    # number or a year does not, because the left-hand piece stands alone.
    ("gap inside one code closes", "Toyota", "7 FGCSU20", [], ["7FGCSU20"]),
    ("series number stays out", "Linde", "H30T 1202", [], ["H30T"]),
    ("series number after a range", "Hyster", "E45-65Z G108", ["E45Z-E65Z"], []),
    ("model year stays out", "Yamaha", "2012 YDREX2", [], ["YDREX2"]),
    ("prefix and numbers split by a gap", "CAT/Mitsubishi", "ETR 230-335",
     ["ETR230-ETR335"], []),
    ("lower-case typo inside a code survives", "Toyota", "7FGu35-80",
     ["7FGU35-7FGU80"], []),

    # ---- separators and noise -------------------------------------------
    ("comma and ampersand both split", "CAT/Mitsubishi", "AF50LP & AF50N7,GP25N",
     [], ["AF50LP", "AF50N7", "GP25N"]),
    ("pipe splits", "CAT/Mitsubishi", "2ET2500-4000 | EFG213-220",
     ["2ET2500-2ET4000", "EFG213-EFG220"], []),
    ("spaced slash starts a new clause", "CAT/Mitsubishi",
     "GC35-50K and GP40-50K / Mitsubishi Forklift FGC35-50K and FG40-50K",
     ["GC35K-GC50K", "GP40K-GP50K", "FGC35K-FGC50K", "FG40K-FG50K"], []),
    ("parenthetical is not fitment", "Nissan/Unicarrier/Logisnext",
     "SCX40N (30-40N)", [], ["SCX40N"]),
    ("trailing mojibake is trimmed", "Hyster", "E55XM�..", [], ["E55XM"]),
    ("model buried after prose", "Stewart & Stevenson", "Rail King RK330",
     [], ["RK330"]),
]

# (label, OEM, Fits:, the token that must be reported rather than read)
UNREADABLE: list[tuple[str, str, str, str]] = [
    ("suffixes too far apart", "CAT/Mitsubishi", "CAT EC22N2-EC30LN2",
     "EC22N2-EC30LN2"),
    ("mismatched suffix pair", "Hyster", "E30HSDS3-E40HSD3", "E30HSDS3-E40HSD3"),
    ("prose", "Nissan/Unicarrier/Logisnext", "Platinum II trucks",
     "Platinum II trucks"),
    ("a date is not a range", "Toyota", "3-20-26 - KLM added the up the the 55",
     "3-20-26 - KLM added the up the the 55"),
    ("mixed-case family name", "Club Car", "CarryAll 300-500",
     "CarryAll 300-500"),
    ("prose around a series word", "Komatsu", "16-SERIES TRUCKS",
     "16-SERIES TRUCKS"),
]


def check_ranges(R) -> None:
    R.start("11 -- kit Fits: ranges, every written form in the notes")
    for label, oem, fits, want_ranges, want_exact in RANGE_CASES:
        got = parse_notes(notes(oem, fits))
        got_ranges = [str(r) for r in got.ranges]
        R.check(
            f"{label}: {fits!r} ranges",
            got_ranges == want_ranges,
            f"wanted {want_ranges}, got {got_ranges}",
        )
        R.check(
            f"{label}: {fits!r} models",
            got.exact == want_exact,
            f"wanted {want_exact}, got {got.exact}",
        )


def check_unreadable(R) -> None:
    R.start("12 -- kit Fits: anything unreadable is reported, never guessed")
    for label, oem, fits, want in UNREADABLE:
        got = parse_notes(notes(oem, fits))
        R.check(
            f"{label}: {fits!r} is reported",
            want in got.unparsed,
            f"unparsed={got.unparsed}",
        )
        R.check(
            f"{label}: {fits!r} invents nothing",
            not got.exact and not got.ranges,
            f"exact={got.exact} ranges={[str(r) for r in got.ranges]}",
        )

    missing_oem = parse_notes("Fits: 8FGCU20-32")
    R.check(
        "no OEM: line yields no designations",
        not missing_oem.readable and missing_oem.unparsed == ["(no OEM: line)"],
        f"oem={missing_oem.oem!r} unparsed={missing_oem.unparsed}",
    )
    missing_fits = parse_notes("OEM: Toyota")
    R.check(
        "no Fits: line yields no designations",
        not missing_fits.readable and missing_fits.unparsed == ["(no Fits: line)"],
        f"unparsed={missing_fits.unparsed}",
    )
    blank = parse_notes("test link")
    R.check("free-text notes yield nothing", not blank.readable, f"{blank}")

    # The caveats hold serial cutoffs, dates and counterpart brands. None of it
    # is the fitment statement, and kit 78's caveat is a Yale code on a Hyster
    # kit -- exactly the link CLAUDE.md forbids.
    caveated = parse_notes(notes("Hyster", "J30-40XN/XNT", "Yale ERP030"))
    R.check(
        "Caveats: is not read as fitment",
        "ERP030" not in caveated.designations(),
        f"designations={caveated.designations()}",
    )


def check_oem_scope(R) -> None:
    R.start("13 -- kit Fits: every designation is scoped to the declared OEM")
    for label, oem, fits, want_ranges, want_exact in RANGE_CASES:
        got = parse_notes(notes(oem, fits))
        R.check(
            f"{label}: OEM carried through",
            all(r.oem == oem for r in got.ranges) and got.oem == oem,
            f"oem={got.oem!r} ranges={[r.oem for r in got.ranges]}",
        )

    # The first pass at this matched `GP40N` inside a CAT/Mitsubishi kit against
    # Yale's GP40N. Both kits exist (kit 7 is CAT, kit 92 is Yale) and they are
    # different trucks, so the OEM has to travel with the code.
    cat = parse_notes(notes("CAT/Mitsubishi", "GP40N-GP55N"))
    yale = parse_notes(notes("Yale", "GP40-70N"))
    R.check(
        "same code under two OEMs stays apart",
        cat.ranges[0].oem != yale.ranges[0].oem,
        f"{cat.ranges[0].oem} vs {yale.ranges[0].oem}",
    )

    # Kit 42 is a Toyota kit whose Fits: line names a Raymond truck. Reading it
    # as Toyota would file a Raymond model under the wrong brand; reading it as
    # Raymond would link a kit the notes never claimed fits one.
    foreign = parse_notes(notes("Toyota", "8BXU | Raymond 730"))
    R.check(
        "a foreign brand is set aside",
        foreign.exact == ["8BXU"] and foreign.foreign == [("Raymond", "Raymond 730")],
        f"exact={foreign.exact} foreign={foreign.foreign}",
    )

    # Kits 77/79/81/82/83/84/86 declare `OEM: Hyster` and then name the Yale
    # counterpart on the same line. Reading the second half as Hyster would
    # invent Hyster-branded rows for Yale trucks and put a Hyster/Yale link one
    # join away -- the link CLAUDE.md forbids outright.
    twinned = [
        ("H135-160FT | S135-160FT | GC/GLC/GDC135-160VX | GP/GLP/GDP135-160VX",
         ["H135FT-H160FT", "S135FT-S160FT"]),
        ("H80-120FT, | GC/GLC/GDC080-120VX | GP/GLP/GDP080-120VX",
         ["H80FT-H120FT"]),
        ("E45-70XN | ERC050VGN36TE083, ERP045-070VL(L)", ["E45XN-E70XN"]),
        ("E80-120XN | ERC080-120VH", ["E80XN-E120XN"]),
        ("E45-65Z G108 | ERC040-065GH", ["E45Z-E65Z"]),
    ]
    for fits, want in twinned:
        got = parse_notes(notes("Hyster", fits))
        R.check(
            f"Hyster kit keeps its own half of {fits[:30]!r}",
            [str(r) for r in got.ranges] == want and not got.exact,
            f"ranges={[str(r) for r in got.ranges]} exact={got.exact}",
        )
        R.check(
            f"Yale half of {fits[:30]!r} is set aside",
            all(oem == "Yale" for oem, _t in got.foreign)
            and not any(d.startswith(("GC", "GLC", "GDC", "GP", "GLP", "GDP",
                                      "ERC", "ERP", "ESC"))
                        for d in got.designations({50})),
            f"foreign={got.foreign} designations={got.designations({50})}",
        )

    # The Hyster prefixes are Yale's own neighbours in the alphabet, so the
    # rule has to be exact rather than a letter test: Hyster's `E45XN` and
    # `E55XM` start with E, as Yale's ERC/ERP/ESC do.
    for model in ("E45XN", "E55XM", "E80XN", "H135FT", "S40FT", "J30XN"):
        own = parse_notes(notes("Hyster", model))
        R.check(f"Hyster {model} is Hyster's own",
                own.exact == [model] and not own.foreign,
                f"exact={own.exact} foreign={own.foreign}")

    # Yale's own kits are untouched: the rule runs one way only, because no
    # Yale kit in the table names a Hyster truck and Hyster's `H`/`S`/`E`/`J`
    # would collide with Yale's `ERC`/`ERP`/`ESC`.
    for fits, want in (("ERC080-120VH", ["ERC080VH-ERC120VH"]),
                       ("GP40-70N", ["GP40N-GP70N"]),
                       ("ERP30-40VT", ["ERP30VT-ERP40VT"])):
        yale_kit = parse_notes(notes("Yale", fits))
        R.check(f"Yale kit keeps {fits!r}",
                [str(r) for r in yale_kit.ranges] == want and not yale_kit.foreign,
                f"ranges={[str(r) for r in yale_kit.ranges]} foreign={yale_kit.foreign}")

    # The sub-brand words are the same catalog manufacturer, so they strip off.
    for word in ("CAT", "MIT", "Mitsubishi"):
        sub = parse_notes(notes("CAT/Mitsubishi", f"{word} FG40-55N1"))
        R.check(
            f"{word!r} is CAT/Mitsubishi's own shorthand",
            [str(r) for r in sub.ranges] == ["FG40N1-FG55N1"],
            f"ranges={[str(r) for r in sub.ranges]} foreign={sub.foreign}",
        )


def check_membership(R) -> None:
    R.start("14 -- kit Fits: range membership and enumeration")
    rng = parse_notes(notes("Toyota", "8FGCU20-32")).ranges[0]
    for model in ("8FGCU20", "8FGCU25", "8FGCU30", "8FGCU32"):
        R.check(f"{model} is inside 8FGCU20-32", rng.contains(model))
    for model in ("8FGCU18", "8FGCU35", "8FGCU20SU", "8FGU25", "8FGCU35U"):
        R.check(f"{model} is outside 8FGCU20-32", not rng.contains(model))

    k5 = parse_notes(notes("CAT/Mitsubishi", "GC35-50K5")).ranges[0]
    R.check("GC40K5 is inside GC35-50K5", k5.contains("GC40K5"))
    R.check("GC40K is outside GC35-50K5 (different generation)",
            not k5.contains("GC40K"), "the K5 suffix is part of the identity")
    R.check("GC60K5 is outside GC35-50K5", not k5.contains("GC60K5"))

    # The notes and the catalog punctuate the same truck differently: kit 69
    # writes `G20-N7`, the catalog row is `G20N-7`.
    doosan = parse_notes(notes("Bobcat/Doosan", "G20-N7 thru G33-N7")).ranges[0]
    for model in ("G20N-7", "G25N-7", "G30N-7", "G33N-7"):
        R.check(f"{model} is inside G20-N7 thru G33-N7", doosan.contains(model))
    R.check("G35N-7 is outside G20-N7 thru G33-N7", not doosan.contains("G35N-7"))

    # Enumeration needs a vocabulary: the interior of a range is the capacities
    # that OEM actually builds, not every integer between the endpoints.
    R.check(
        "endpoints are members with no vocabulary at all",
        rng.members(set()) == ["8FGCU20", "8FGCU32"],
        f"{rng.members(set())}",
    )
    R.check(
        "vocabulary fills the interior and nothing outside it",
        rng.members({15, 18, 25, 30, 35, 70})
        == ["8FGCU20", "8FGCU25", "8FGCU30", "8FGCU32"],
        f"{rng.members({15, 18, 25, 30, 35, 70})}",
    )
    padded = parse_notes(notes("Yale", "ERC030-040VAN")).ranges[0]
    R.check(
        "members keep the written zero padding",
        padded.members({35}) == ["ERC030VAN", "ERC035VAN", "ERC040VAN"],
        f"{padded.members({35})}",
    )

    # A series-level claim ("FC5200 Series") names no single truck, so it is
    # kept apart from the designations rather than expanded into them.
    crown = parse_notes(notes("Crown", "FC5200 Series | E18-20 (346)"))
    R.check(
        "a Series claim is held separately",
        crown.series_refs == ["FC5200"] and [str(r) for r in crown.ranges] == ["E18-E20"],
        f"series={crown.series_refs} ranges={[str(r) for r in crown.ranges]}",
    )
    R.check(
        "a Series claim is not a designation",
        "FC5200" not in crown.designations({19}),
        f"{crown.designations({19})}",
    )


def check_real_rows(R) -> None:
    R.start("15 -- kit Fits: the notes as they are actually written")
    # Verbatim from the kits table, with what each one has to come out as.
    cases: list[tuple[str, str, list[str], list[str]]] = [
        ("Toyota", "8FGCU20-32, 8FGCU35U-70U, 8FGU20-32, 8FG35U-80U, 8FDU20-32",
         ["8FGCU20-8FGCU32", "8FGCU35U-8FGCU70U", "8FGU20-8FGU32",
          "8FG35U-8FG80U", "8FDU20-8FDU32"], []),
        ("Hyster",
         "H135-160FT | S135-160FT | GC/GLC/GDC135-160VX | GP/GLP/GDP135-160VX",
         ["H135FT-H160FT", "S135FT-S160FT"], []),
        ("CAT/Mitsubishi", "GC35-50K5 and GP40-50K5",
         ["GC35K5-GC50K5", "GP40K5-GP50K5"], []),
        ("CAT/Mitsubishi", "CAT EC15-18LN3, MIT FBC15-18LN3",
         ["EC15LN3-EC18LN3", "FBC15LN3-FBC18LN3"], []),
        ("Bobcat/Doosan", "BC20-32S-7 (elec)", ["BC20S-7-BC32S-7"], []),
        ("Hyster", "E45-65Z G108 | ERC040-065GH", ["E45Z-E65Z"], []),
        ("Hyundai", "15-23BRP-9", ["15BRP-9-23BRP-9"], []),
        ("Heli", "CPYD25C-KOMATSU", [], ["CPYD25C-KOMATSU"]),
    ]
    for oem, fits, want_ranges, want_exact in cases:
        got = parse_notes(notes(oem, fits))
        R.check(
            f"{oem} {fits[:44]!r}",
            [str(r) for r in got.ranges] == want_ranges and got.exact == want_exact,
            f"ranges={[str(r) for r in got.ranges]} exact={got.exact} "
            f"unparsed={got.unparsed}",
        )

    # A hand-built range stands on its own, independent of any parsing.
    hand = ModelRange("Toyota", "8FGCU", "", 20, 32, 2, "test")
    R.check("a range renders its own endpoints", str(hand) == "8FGCU20-8FGCU32", str(hand))


def run(R) -> None:
    check_ranges(R)
    check_unreadable(R)
    check_oem_scope(R)
    check_membership(R)
    check_real_rows(R)


def main() -> int:
    from stress_lookup import R  # reuse the house scoreboard

    run(R)
    return R.summary()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
