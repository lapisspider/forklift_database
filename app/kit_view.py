"""Assembling the kit-centric pages.

The kit is the spine now, so these are the two shapes the templates need and
neither is a plain query:

`kit_summaries()` -- one row per kit for the list, carrying every OEM it fits
(so a kit can appear under all of them), how many trucks it has actually gone
on, and whether it needs a person's attention.

`kit_models()` -- one block per (OEM, model expression) for the detail page,
with the documented rule and the observed rule side by side rather than merged.
Keeping them apart is the whole point of the `origin` split: a range that has
drifted somewhere wrong has to look different from one that was always right.

Lives apart from app/fitment.py because it reads app/fitment_derive.py, which
reads app/fitment.py -- and a view helper is the right place for that knot to
be, not the lookup.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from . import fitment, fitment_derive
from .models import Kit, KitFitment, SerialRecord

# Kit-list sorts. The default is by the OEM of the truck, because a salesperson
# arrives knowing the truck's brand and not the kit's.
SORTS = {
    "oem": "Truck manufacturer",
    "sku": "Part number",
    "recent": "Most recently confirmed",
    "installs": "Most trucks confirmed",
    "attention": "Needs attention",
}


@dataclass
class KitSummary:
    kit: Kit
    oems: list[str] = field(default_factory=list)
    fitments: list[KitFitment] = field(default_factory=list)
    record_count: int = 0
    latest_record: datetime | None = None
    attention: list[str] = field(default_factory=list)

    @property
    def model_exprs(self) -> list[str]:
        return sorted({f.model_expr for f in self.fitments})

    @property
    def has_serial_rule(self) -> bool:
        return any(f.condition != "always" for f in self.fitments)


@dataclass
class ModelFit:
    """One (OEM, model expression) on a kit's detail page."""
    manufacturer: str
    model_expr: str
    documented: list[KitFitment] = field(default_factory=list)
    observed: list[KitFitment] = field(default_factory=list)
    records: list[SerialRecord] = field(default_factory=list)

    @property
    def rules(self) -> list[KitFitment]:
        return self.documented + self.observed

    @property
    def beyond_documented(self) -> bool:
        """An observed range reaching past what the sheet documented. Worth
        saying out loud: it is either a sheet that is out of date or a kit being
        fitted where it was never signed off."""
        for doc in self.documented:
            if doc.condition != "range":
                continue
            for obs in self.observed:
                if obs.serial_scope != doc.serial_scope:
                    continue
                if doc.serial_lo is not None and obs.serial_lo is not None \
                        and obs.serial_lo < doc.serial_lo:
                    return True
                if doc.serial_hi is not None and obs.serial_hi is not None \
                        and obs.serial_hi > doc.serial_hi:
                    return True
        return False


def _attention(db: Session) -> dict[int, list[str]]:
    """Per-kit reasons a person should look, from the derivation pass."""
    report = fitment_derive.analyse(db)
    out: dict[int, list[str]] = defaultdict(list)
    for env, doc in report.beyond_documented:
        out[env.kit_id].append(
            f"{env.manufacturer} {env.model_expr}: the sheet says "
            f"{doc.serial_display}, order records reach {env.lo}-{env.hi}")
    skus = {k.sku: k.id for k in db.query(Kit).all()}
    for c in report.conflicts:
        for sku in c.skus:
            kid = skus.get(sku)
            if kid is not None:
                out[kid].append(
                    f"{c.manufacturer} {c.model} serial {c.serial} is also "
                    f"claimed by {', '.join(s for s in c.skus if s != sku)}")
    # report.unlinked has no kit to hang on; the list page shows that count.
    return out


def kit_summaries(db: Session, sort: str = "oem") -> list[KitSummary]:
    """One row per kit, in the requested order."""
    summaries: dict[int, KitSummary] = {
        k.id: KitSummary(kit=k) for k in db.query(Kit).order_by(Kit.sku).all()
    }
    for fit in db.query(KitFitment).all():
        s = summaries.get(fit.kit_id)
        if s is None:
            continue
        s.fitments.append(fit)
        if fit.manufacturer not in s.oems:
            s.oems.append(fit.manufacturer)
    for rec in db.query(SerialRecord).filter(
            SerialRecord.status == "active", SerialRecord.kit_id.isnot(None)).all():
        s = summaries.get(rec.kit_id)
        if s is None:
            continue
        s.record_count += 1
        if s.latest_record is None or (rec.recorded_at and rec.recorded_at > s.latest_record):
            s.latest_record = rec.recorded_at

    attention = _attention(db)
    rows = list(summaries.values())
    for s in rows:
        s.oems.sort()
        s.attention = attention.get(s.kit.id, [])

    if sort == "sku":
        rows.sort(key=lambda s: s.kit.sku)
    elif sort == "recent":
        # Never confirmed sorts last, not first: a null is not a recent date.
        rows.sort(key=lambda s: (s.latest_record is None,
                                 -(s.latest_record.timestamp() if s.latest_record else 0),
                                 s.kit.sku))
    elif sort == "installs":
        rows.sort(key=lambda s: (-s.record_count, s.kit.sku))
    elif sort == "attention":
        rows.sort(key=lambda s: (not s.attention, -len(s.attention), s.kit.sku))
    else:
        rows.sort(key=lambda s: (s.oems[0] if s.oems else "zzz", s.kit.sku))
    return rows


def grouped_by_oem(rows: list[KitSummary]) -> dict[str, list[KitSummary]]:
    """The default view: a kit appears under EVERY OEM it fits, not just the
    first. Kits with no readable fitment go under a bucket of their own rather
    than vanishing -- there are 16 of them and they are the ones that need work.
    """
    out: dict[str, list[KitSummary]] = {}
    for s in rows:
        for oem in (s.oems or ["(no fitment on record)"]):
            out.setdefault(oem, []).append(s)
    return dict(sorted(out.items(),
                       key=lambda kv: (kv[0].startswith("("), kv[0])))


def kit_models(db: Session, kit: Kit) -> list[ModelFit]:
    """The detail page's blocks: documented and observed kept side by side."""
    blocks: dict[tuple[str, str], ModelFit] = {}
    for fit in sorted(kit.fitments, key=lambda f: (f.manufacturer, f.model_expr)):
        key = (fit.manufacturer, fit.model_expr)
        block = blocks.setdefault(key, ModelFit(fit.manufacturer, fit.model_expr))
        (block.observed if fit.origin == "observed" else block.documented).append(fit)

    for rec in sorted(kit.serial_records, key=lambda r: (r.manufacturer, r.model, r.id)):
        if rec.status != "active":
            continue
        placed = False
        for (oem, expr), block in blocks.items():
            if oem == rec.manufacturer and fitment.expr_matches(oem, expr, rec.model):
                block.records.append(rec)
                placed = True
        if not placed:
            # A record whose model no rule names. It is still this kit's, and
            # hiding it would hide the most interesting thing on the page.
            key = (rec.manufacturer, rec.model)
            block = blocks.setdefault(key, ModelFit(rec.manufacturer, rec.model))
            block.records.append(rec)
    return [blocks[k] for k in sorted(blocks)]
