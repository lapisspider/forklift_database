"""Whether two serial numbers can be compared, and where one sits in a range.

This is the piece the kit-fitment rules rest on, and the one most able to do
quiet damage. `174-MCA1-00G05-01` fits a GP40-50K up to serial 10230 and
`174-MCA2-00G05-01` from 10231 on, so a salesperson is quoted a different part
on the strength of a `>=` comparison. If that comparison is run against a
serial scheme that does not actually count upwards, the tool recommends the
wrong kit and gives no sign of it.

So the rule here is: **a comparison is refused unless the scheme is known to be
ordered.** `in_range()` returns None for "cannot say", which is a different
answer from False and callers must treat it as such.

Three things make a serial uncomparable, and all three are real in this data:

1. **The OEM's numbering is not chronological.** Bobcat/Doosan allocates in
   batches -- a higher sequence can predate a lower one (CLAUDE.md, and
   app/ai/decoders/doosan.py says the same). `serial >= 10231` means nothing
   there, so Doosan is listed as unordered and only exact serial matching is
   allowed.
2. **The sequence only counts within a scope.** A Hyster/Yale serial is
   prefix(4) + plant(1) + sequence + year(1), and the prefix names the design
   generation and model family. Sequence 01515 under prefix G004 has no
   relationship to 01515 under H177, so both points carry a scope and a
   comparison across scopes is refused.
3. **The OEM is simply not on file.** Most are. An unknown scheme returns None
   rather than falling back to string or integer order -- a plain `int()` of
   whatever digits appear is exactly the kind of guess that reads as confident
   and is wrong.

Adding an OEM means evidence, not inference: a documented serial structure, or
a range SSC already relies on. CAT/Mitsubishi is here because the kit sheet
states `serial # 00001-10230` and `10231+` as a plain running number.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# How an OEM's serials are structured. "unordered" means the numbers exist but
# do not count upwards in time, which is worse than having none.
ORDERED = "ordered"
UNORDERED = "unordered"
UNKNOWN = "unknown"

# Hyster/Yale: G004V01515D -> prefix G004, plant V, sequence 01515, year D.
# Mirrors serial_decode.SERIAL_RE; kept local so this module does not need a
# database session just to read a number out of a string.
_HY_RE = re.compile(
    r"^(?P<prefix>[A-Z][0-9][A-Z0-9][0-9])(?P<plant>[A-Z])(?P<seq>[0-9]{3,7})(?P<year>[A-Z])$"
)
_ALL_DIGITS = re.compile(r"^[0-9]+$")
_SEPARATORS = re.compile(r"[\s\-_/.#]")

_SCHEMES: dict[str, str] = {
    "CAT/Mitsubishi": ORDERED,      # the kit sheet ranges on it: 00001-10230, 10231+
    "Hyster": ORDERED,              # within one prefix; see scope below
    "Yale": ORDERED,                # same scheme as Hyster
    "Bobcat/Doosan": UNORDERED,     # batch-allocated; CLAUDE.md and decoders/doosan.py
}


@dataclass(frozen=True)
class SerialPoint:
    """A serial reduced to something comparable.

    `scope` is the part of the serial a sequence counts WITHIN -- the
    Hyster/Yale prefix, or "" where the OEM runs one sequence. Two points are
    only comparable when their scopes match.
    """
    scope: str
    sequence: int


def normalize_serial(serial: str | None) -> str:
    """Upper-case, separators removed. `L7-00116` and `l7 00116` are one string."""
    return _SEPARATORS.sub("", (serial or "")).upper()


def scheme(manufacturer: str | None) -> str:
    return _SCHEMES.get((manufacturer or "").strip(), UNKNOWN)


def scheme_is_ordered(manufacturer: str | None) -> bool:
    """True only when this OEM's serials are known to count upwards."""
    return scheme(manufacturer) == ORDERED


def serial_point(manufacturer: str | None, serial: str | None) -> SerialPoint | None:
    """A comparable point, or None when this serial cannot be placed.

    None covers every uncertain case -- an unordered or unknown OEM, a serial
    that does not fit the OEM's shape, an empty string. Callers must read None
    as "cannot say", never as "does not match".
    """
    if not scheme_is_ordered(manufacturer):
        return None
    raw = normalize_serial(serial)
    if not raw:
        return None

    oem = (manufacturer or "").strip()
    if oem in ("Hyster", "Yale"):
        m = _HY_RE.match(raw)
        if not m:
            return None
        # The prefix is the scope: a sequence only counts within one design
        # generation and model family.
        return SerialPoint(scope=m.group("prefix"), sequence=int(m.group("seq")))

    if oem == "CAT/Mitsubishi":
        if not _ALL_DIGITS.match(raw):
            return None
        return SerialPoint(scope="", sequence=int(raw))

    return None


def comparable(a: SerialPoint | None, b: SerialPoint | None) -> bool:
    """Two points can be ordered against each other only within one scope."""
    return a is not None and b is not None and a.scope == b.scope


def in_range(manufacturer: str | None, serial: str | None,
             lo: int | None, hi: int | None, scope: str = "") -> bool | None:
    """Is this serial inside [lo, hi]? None means the question cannot be answered.

    `hi` of None is an open-ended range ("10231+"). `lo` of None is open at the
    bottom. Both None means the rule does not constrain the serial at all, which
    is True for any serial this OEM can place -- and still None for one it
    cannot, because we would be claiming a match we cannot support.
    """
    point = serial_point(manufacturer, serial)
    if point is None:
        return None
    if point.scope != scope:
        return None
    if lo is not None and point.sequence < lo:
        return False
    if hi is not None and point.sequence > hi:
        return False
    return True


def explain(manufacturer: str | None) -> str:
    """One sentence for the UI when a serial cannot be used, in plain terms."""
    s = scheme(manufacturer)
    if s == ORDERED:
        return "Serial numbers for this manufacturer are in a known running order."
    if s == UNORDERED:
        return (f"{manufacturer} allocates serial numbers in batches, so a higher "
                f"number can be an older truck. Serial ranges cannot be used here — "
                f"only an exact serial already on record.")
    return (f"No serial-number scheme is on file for {manufacturer or 'this manufacturer'}, "
            f"so a serial cannot be placed in a range. Record the serial and it can be "
            f"matched exactly.")
