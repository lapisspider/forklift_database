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


def decode(db: Session, norm: str) -> SerialDecodeResult:
    return serial_decode.decode(db, norm)


class _HysterYaleDecoder:
    name = name
    manufacturers = manufacturers
    match = staticmethod(match)
    decode = staticmethod(decode)


DECODER = _HysterYaleDecoder()
