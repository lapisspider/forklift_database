"""Shared types for the decoder registry, split out from registry.py so the
individual decoder modules (doosan.py, hyster_yale.py) can import Candidate
without a circular import back through registry.py."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from ...schemas import SerialDecodeResult

_NORM = re.compile(r"[\s\-_/.]")


def normalize(raw: str) -> str:
    return _NORM.sub("", raw or "").upper()


@dataclass
class Candidate:
    decoder: str
    kind: str
    confidence: float
    reason: str


class Decoder(Protocol):
    name: str
    manufacturers: tuple[str, ...]

    def match(self, db: Session, norm: str) -> Candidate | None: ...
    def decode(self, db: Session, norm: str) -> SerialDecodeResult: ...
