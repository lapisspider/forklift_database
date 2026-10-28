"""Turning order records into fitment ranges.

An order record is truth: order management records the kit that was genuinely
supplied, so a serial landing outside a documented range widens it rather than
contradicting it. This pass is what does the widening.

WHAT IT WILL NOT DO
-------------------

**It never edits a `documented` row.** The plan's requirement is that you can
always see which part of a range came from the sheet and which came from the
field, and that is gone the moment the two are added together. So the documented
row is left exactly as the sheet wrote it and the observed envelope is kept
beside it as its own `observed` row. The effective range a salesperson sees is
the union of the two, and `app/fitment.py` reports each hit separately, so a
rule that has drifted far past its documented bound is visible rather than
blended away.

**It never narrows.** Observed rows only ever grow. The absence of a record at
some serial is not evidence that the kit does not fit there -- it is the much
more likely evidence that nobody has ordered one yet.

**It never ranges an unordered scheme.** Bobcat/Doosan allocates serials in
batches, so `>= 10231` means nothing there (app/serials.py). Records on an
unordered or unknown scheme produce no range at all; they are still recorded,
and `app/fitment.py` matches them by exact serial instead, which is stronger
evidence than any range and needs no scheme.

Observed rows are rebuilt from scratch on every pass -- they are derived data,
and recomputing is the only way a superseded record actually stops counting.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import fitment, serials
from .models import Kit, KitFitment, SerialRecord

DERIVED_BY = "derive"          # stamped on confirmed_by so the pass owns its rows


@dataclass
class Envelope:
    """The observed span of one (kit, OEM, model expression, serial scope)."""
    kit_id: int
    manufacturer: str
    model_expr: str
    scope: str
    lo: int
    hi: int
    count: int = 0
    lo_serial: str = ""
    hi_serial: str = ""
    models: set[str] = field(default_factory=set)

    def widen(self, seq: int, serial: str, model: str) -> None:
        if seq < self.lo:
            self.lo, self.lo_serial = seq, serial
        if seq > self.hi:
            self.hi, self.hi_serial = seq, serial
        self.count += 1
        self.models.add(model)


@dataclass
class Conflict:
    """Two kits both claiming one truck -- for the 'needs attention' view."""
    manufacturer: str
    model: str
    serial: str
    skus: list[str]


@dataclass
class DeriveReport:
    envelopes: list[Envelope] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    beyond_documented: list[tuple[Envelope, KitFitment]] = field(default_factory=list)
    unplaceable: list[SerialRecord] = field(default_factory=list)
    unlinked: list[SerialRecord] = field(default_factory=list)
    no_rule: list[Envelope] = field(default_factory=list)
    removed: int = 0
    written: int = 0


def _active(db: Session) -> list[SerialRecord]:
    return (db.query(SerialRecord)
            .filter(SerialRecord.status == "active")
            .order_by(SerialRecord.id)
            .all())


def analyse(db: Session) -> DeriveReport:
    """Work out what the records say. Writes nothing."""
    report = DeriveReport()
    rules = db.query(KitFitment).filter(KitFitment.origin == "documented").all()
    by_kit: dict[int, list[KitFitment]] = defaultdict(list)
    for r in rules:
        by_kit[r.kit_id].append(r)

    envelopes: dict[tuple, Envelope] = {}
    claims: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for rec in _active(db):
        if rec.kit_id is None:
            report.unlinked.append(rec)
            continue

        # Which documented rule does this record land on? The record names a
        # single truck; the rules name expressions.
        matched = [r for r in by_kit.get(rec.kit_id, [])
                   if r.manufacturer == rec.manufacturer
                   and fitment.expr_matches(rec.manufacturer, r.model_expr, rec.model)]

        if rec.serial_norm:
            claims[(rec.manufacturer, rec.model, rec.serial_norm)].add(
                rec.part_number)

        point = serials.serial_point(rec.manufacturer, rec.serial)
        if point is None:
            # Recorded, but it cannot widen anything. The exact-serial match in
            # app/fitment.py is what makes this record useful.
            report.unplaceable.append(rec)
            continue

        # An expression to hang the envelope on: the documented rule's wording
        # where there is one, else the record's own model.
        exprs = [r.model_expr for r in matched] or [rec.model]
        for expr in exprs:
            key = (rec.kit_id, rec.manufacturer, expr, point.scope)
            env = envelopes.get(key)
            if env is None:
                env = Envelope(kit_id=rec.kit_id, manufacturer=rec.manufacturer,
                               model_expr=expr, scope=point.scope,
                               lo=point.sequence, hi=point.sequence,
                               lo_serial=rec.serial, hi_serial=rec.serial)
                envelopes[key] = env
            env.widen(point.sequence, rec.serial, rec.model)

    for (oem, model, serial), skus in claims.items():
        if len(skus) > 1:
            report.conflicts.append(
                Conflict(oem, model, serial, sorted(skus)))

    report.envelopes = sorted(envelopes.values(),
                              key=lambda e: (e.kit_id, e.manufacturer, e.model_expr))

    # Which envelopes say something the sheet did not?
    for env in report.envelopes:
        docs = [r for r in by_kit.get(env.kit_id, [])
                if r.manufacturer == env.manufacturer
                and r.model_expr == env.model_expr]
        if not docs:
            report.no_rule.append(env)
            continue
        for doc in docs:
            if doc.condition != "range":
                continue
            if (doc.serial_lo is not None and env.lo < doc.serial_lo) or \
               (doc.serial_hi is not None and env.hi > doc.serial_hi):
                report.beyond_documented.append((env, doc))
    return report


def _note(env: Envelope) -> str:
    models = ", ".join(sorted(env.models))
    if env.count == 1:
        return (f"1 order record: serial {env.hi_serial} ({models}). "
                f"Observed, not from the kit sheet.")
    return (f"{env.count} order records: serials {env.lo_serial} to "
            f"{env.hi_serial} ({models}). Observed, not from the kit sheet.")


def apply(db: Session, report: DeriveReport | None = None) -> DeriveReport:
    """Rebuild the `observed` rows from the records. Returns what it did."""
    report = report or analyse(db)

    stale = (db.query(KitFitment)
             .filter(KitFitment.origin == "observed",
                     KitFitment.confirmed_by == DERIVED_BY)
             .all())
    report.removed = len(stale)
    for row in stale:
        db.delete(row)
    db.flush()

    now = datetime.now(timezone.utc)
    for env in report.envelopes:
        db.add(KitFitment(
            kit_id=env.kit_id, manufacturer=env.manufacturer,
            model_expr=env.model_expr, condition="range",
            serial_lo=env.lo, serial_hi=env.hi, serial_scope=env.scope,
            origin="observed", source_note=_note(env),
            confirmed_by=DERIVED_BY, confirmed_at=now,
        ))
        report.written += 1
    db.commit()
    return report


def describe(db: Session, report: DeriveReport) -> list[str]:
    """The pass as lines for a console or a 'needs attention' page."""
    skus = {k.id: k.sku for k in db.query(Kit).all()}
    out = [
        f"order records considered: {len(_active(db))}",
        f"observed ranges derived:  {len(report.envelopes)}",
        f"records with no kit link: {len(report.unlinked)}",
        f"records with an unplaceable serial: {len(report.unplaceable)} "
        f"(matched exactly instead, never ranged)",
    ]
    if report.no_rule:
        out.append(f"\nobserved ranges with no documented rule behind them "
                   f"({len(report.no_rule)}):")
        for env in report.no_rule:
            out.append(f"    {skus.get(env.kit_id, env.kit_id):<22} "
                       f"{env.manufacturer:<16} {env.model_expr:<16} "
                       f"{env.lo}-{env.hi}  ({env.count} records)")
    if report.beyond_documented:
        out.append(f"\nNEEDS ATTENTION -- observed past the documented bound "
                   f"({len(report.beyond_documented)}):")
        for env, doc in report.beyond_documented:
            out.append(f"    {skus.get(env.kit_id, env.kit_id):<22} "
                       f"{env.manufacturer} {env.model_expr}: "
                       f"sheet says {doc.serial_display}, records reach "
                       f"{env.lo}-{env.hi}")
    if report.conflicts:
        out.append(f"\nNEEDS ATTENTION -- one truck, several kits "
                   f"({len(report.conflicts)}):")
        for c in report.conflicts:
            out.append(f"    {c.manufacturer} {c.model} serial {c.serial}: "
                       f"{', '.join(c.skus)}")
    return out
