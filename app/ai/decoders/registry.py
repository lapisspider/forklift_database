"""Registry of per-OEM serial decoders. detect() runs every decoder's cheap
`match()` and ranks the results; the caller then calls `.decode()` on
whichever one(s) it wants to render."""
from __future__ import annotations

from sqlalchemy.orm import Session

from . import doosan, hyster_yale
from .types import Candidate, Decoder, normalize

__all__ = ["Candidate", "Decoder", "normalize", "DECODERS", "TIE_BAND", "detect"]

DECODERS: list[Decoder] = [hyster_yale.DECODER, doosan.DECODER]

TIE_BAND = 0.15


def detect(db: Session, norm: str, manufacturer: str | None = None) -> list[Candidate]:
    """Run every applicable decoder's match() and rank the results:
    highest confidence first, ties broken by longer normalized input, then
    by registry order (stable sort preserves DECODERS order on exact ties)."""
    out: list[Candidate] = []
    for d in DECODERS:
        if manufacturer and manufacturer not in d.manufacturers:
            continue
        c = d.match(db, norm)
        if c is not None:
            out.append(c)
    out.sort(key=lambda c: (-c.confidence, -len(norm)))
    return out
