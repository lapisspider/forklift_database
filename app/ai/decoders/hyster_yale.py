"""Thin adapter over app/ai/serial_decode.py (the actual Hyster/Yale decode
logic lives there, unchanged) so it can sit in the multi-OEM registry."""
from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ...models import SerialPrefix
from ...schemas import SerialDecodeResult
from .. import serial_decode
from .types import Candidate

name = "hyster_yale"
manufacturers = ("Hyster", "Yale")

_ALPHA_KEY_RE = re.compile(r"^(H|J|A[B-K])")

# A string shaped like the 11-character serial but with the sequence or the year
# letter wrong -- i.e. a typo, not another OEM's number. Deliberately wider than
# SERIAL_RE on the sequence (3-7 digits) so an off-by-one is caught and named.
_NEAR_MISS_RE = re.compile(
    r"^(?P<prefix>[A-Z][0-9][A-Z0-9][0-9])(?P<plant>[A-Z])(?P<seq>[0-9]{3,7})(?P<year>[A-Z])$"
)
_UNUSED_YEAR_LETTERS = "IOQ"


def _prefix_known(db: Session, prefix: str) -> bool:
    return db.query(SerialPrefix).filter(SerialPrefix.prefix == prefix).first() is not None


def match(db: Session, norm: str) -> Candidate | None:
    kind = serial_decode.classify(norm)
    if kind is None:
        return None

    if kind == "serial":
        m = serial_decode.SERIAL_RE.match(norm)
        prefix = m.group("prefix")
        if _prefix_known(db, prefix):
            return Candidate(name, kind, 1.00, f"full Hyster/Yale serial shape, prefix {prefix} on file")
        return Candidate(name, kind, 0.80, f"full Hyster/Yale serial shape, prefix {prefix} not on file")

    if kind == "prefix":
        if _prefix_known(db, norm):
            return Candidate(name, kind, 0.60, f"bare prefix {norm} on file")
        return Candidate(name, kind, 0.35, f"bare prefix {norm} shape only, not on file")

    if kind == "yale_pre1995":
        am = _ALPHA_KEY_RE.match(norm)
        if am:
            key = am.group(1)
            if any(r["era"] == "1958-1968" and r["key"] == key for r in serial_decode._PRE1995):
                # Slightly under 1.00: this legacy format is a reference explainer,
                # never validated against the catalog the way a modern serial is --
                # discounted so a same-shape collision (e.g. Doosan H7-...) can win.
                return Candidate(name, kind, 0.90, f"pre-1995 Yale alpha-year prefix {key} on file")
        return Candidate(name, kind, 0.35, "pre-1995 Yale numeric serial, ambiguous era (regex only)")

    return None


def reject_reason(db: Session, norm: str) -> str | None:
    """Explain a near-miss of the 11-character format, or None.

    A serial that cannot exist -- a 4- or 6-digit sequence, or a year letter of
    I, O or Q, none of which the 23-letter cycle uses -- is a typo. Naming the
    mistake is both cheaper and more useful than spending a web search to come
    back with an unverified guess.
    """
    if serial_decode.classify(norm) is not None:
        return None  # it matched a real format; nothing to explain
    m = _NEAR_MISS_RE.match(norm)
    if not m:
        return None

    seq, year = m.group("seq"), m.group("year")
    problems = []
    if len(seq) != 5:
        problems.append(f"the production sequence is exactly 5 digits and this has {len(seq)}")
    if year in _UNUSED_YEAR_LETTERS:
        problems.append(f"the year letter is never I, O or Q, and this ends in {year}")
    elif year not in serial_decode.YEAR:
        problems.append(f"{year} is not one of the 23 year letters")
    if not problems:
        return None

    return (f"{norm} can't be a Hyster/Yale serial: " + "; ".join(problems) + ". The format is "
            "a 4-character prefix, a plant letter, a 5-digit sequence and a year letter -- "
            "11 characters, as in A269V01501P. Check the data plate.")


def decode(db: Session, norm: str) -> SerialDecodeResult:
    return serial_decode.decode(db, norm)


class _HysterYaleDecoder:
    name = name
    manufacturers = manufacturers
    match = staticmethod(match)
    decode = staticmethod(decode)
    reject_reason = staticmethod(reject_reason)


DECODER = _HysterYaleDecoder()
