"""`(OEM, model, serial) -> kits`: the question a salesperson actually asks.

The old question was `model -> kits`, and `kit_forklift` answers it. It is no
longer enough: `174-MCA1-00G05-01` and `174-MCA2-00G05-01` both fit a GP40-50K
and only the serial separates them, so a lookup that ignores the serial returns
two kits and no way to choose. This module answers with the serial included.

Every hit carries a verdict, and the three are kept apart on purpose:

  confirmed   -- a rule names this model and the serial falls inside it.
  excluded    -- a rule names this model and the serial falls OUTSIDE it. This
                 is a real negative and worth showing: it is the reason the
                 other kit is the right one.
  unconfirmed -- the serial cannot be placed against this rule. An unordered
                 OEM scheme, an unreadable serial, a condition only a person can
                 read, or no serial supplied at all. Never shown as a fit, never
                 shown as a miss.

The ruling this follows: when nothing confirms, the salesperson still sees the
model's kits, clearly marked unconfirmed for that serial, rather than an empty
result that reads as "no kit exists".
"""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import lru_cache

from sqlalchemy.orm import Session

from . import kit_ranges, serials
from .models import Kit, KitFitment, SerialRecord

# A record of this exact truck having been supplied this kit. Stronger than any
# range -- it is the thing ranges are inferred FROM -- and it is the only verdict
# available on an OEM whose serials cannot be ordered at all.
SUPPLIED = "supplied"
CONFIRMED = "confirmed"
EXCLUDED = "excluded"
UNCONFIRMED = "unconfirmed"

_ORDER = {SUPPLIED: 0, CONFIRMED: 1, UNCONFIRMED: 2, EXCLUDED: 3}


@dataclass
class FitHit:
    """One kit weighed against a truck.

    `fitment` is the rule behind the verdict, and is None for a hit that rests
    on an order record alone -- a kit supplied to this exact truck for which no
    fitment rule has been written yet.
    """
    kit: Kit
    fitment: KitFitment | None
    verdict: str
    reason: str
    records: list[SerialRecord] = dc_field(default_factory=list)

    @property
    def sku(self) -> str:
        return self.kit.sku


@lru_cache(maxsize=2048)
def _expr_names(oem: str, model_expr: str) -> tuple[frozenset[str], tuple]:
    """Read one stored `model_expr` back into exact codes and ranges.

    Goes through `kit_ranges.parse_notes` rather than a private helper, so the
    expression is read by exactly the same code that read it off the kit sheet.
    Cached because a lookup weighs many rules and the expressions repeat.
    """
    fits = kit_ranges.parse_notes(f"OEM: {oem}\nFits: {model_expr}")
    return frozenset(fits.exact), tuple(fits.ranges)


def expr_matches(oem: str, model_expr: str, model: str) -> bool:
    """Does this stored expression name that model? `GC35-50K` names GC40K.

    Two shapes of query both have to work. A salesperson types a single truck
    (`GP40K`), which has to be found inside the range the sheet wrote. But the
    sheet's own wording (`GP40-50K`) is also what gets pasted in from a kit
    listing, and an expression names itself -- so that is matched first, before
    anything is decomposed.
    """
    wanted = kit_ranges.normalise(model)
    if kit_ranges.normalise(model_expr) == wanted:
        return True
    exact, ranges = _expr_names(oem, model_expr)
    # Punctuation-insensitive on the code, same as ModelRange.contains -- the
    # notes and the catalog punctuate one truck differently (G20-N7 vs G20N-7).
    if any(kit_ranges.normalise(e) == wanted for e in exact):
        return True
    return any(rng.contains(model) for rng in ranges)


def _weigh(fit: KitFitment, serial: str | None) -> tuple[str, str]:
    """A verdict and the sentence explaining it, for one rule."""
    if fit.condition == "always":
        return CONFIRMED, "fits the whole model; no serial condition on record"

    if not serial:
        return UNCONFIRMED, f"needs a serial to check ({fit.serial_display})"

    if fit.condition == "pattern":
        return UNCONFIRMED, (
            f"the serial condition on record is '{fit.serial_display}', "
            f"which has to be read by a person")

    verdict = fit.matches_serial(serial)
    if verdict is True:
        # "within 00001-10230" for a numeric bound, "in series R177" for a rule
        # that only names the series -- "within series R177" reads as a range.
        preposition = "within" if fit.bounded else "in"
        return CONFIRMED, f"serial is {preposition} {fit.serial_display}"
    if verdict is False:
        return EXCLUDED, f"serial is outside {fit.serial_display}"

    # None: the comparison could not be made. Say which of the reasons it was,
    # because "we cannot tell" is useless without it.
    if not serials.scheme_is_ordered(fit.manufacturer):
        return UNCONFIRMED, serials.explain(fit.manufacturer)
    point = serials.serial_point(fit.manufacturer, serial)
    if point is None:
        return UNCONFIRMED, (
            f"'{serial}' does not read as a {fit.manufacturer} serial number, "
            f"so it cannot be placed in {fit.serial_display}")
    return UNCONFIRMED, (
        f"this rule applies to serial prefix {fit.serial_scope}; "
        f"'{serial}' is prefix {point.scope}")


def records_for(db: Session, manufacturer: str, model: str,
                serial: str | None) -> list[SerialRecord]:
    """Order records for this exact truck. Matched on the normalised serial, so
    `L7-00116` and `l7 00116` are one truck."""
    if not serial:
        return []
    norm = serials.normalize_serial(serial)
    if not norm:
        return []
    return (db.query(SerialRecord)
            .filter(SerialRecord.status == "active",
                    SerialRecord.manufacturer == manufacturer,
                    SerialRecord.serial_norm == norm)
            .order_by(SerialRecord.recorded_at)
            .all())


def kits_for(db: Session, manufacturer: str, model: str,
             serial: str | None = None) -> list[FitHit]:
    """Every kit with a rule naming this truck, strongest evidence first.

    An empty list means nothing names the model at all -- which is different
    from "no kit fits it", and the caller must say so in those words.
    """
    rows = (
        db.query(KitFitment)
        .filter(KitFitment.manufacturer == manufacturer)
        .all()
    )
    hits: list[FitHit] = []
    for fit in rows:
        if not expr_matches(manufacturer, fit.model_expr, model):
            continue
        verdict, reason = _weigh(fit, serial)
        hits.append(FitHit(kit=fit.kit, fitment=fit, verdict=verdict, reason=reason))

    # An order record for this exact truck outranks every rule, including a rule
    # that excludes it: the kit demonstrably went on this machine. Jacob's
    # ruling -- an order record is truth -- lives here.
    by_sku: dict[str, FitHit] = {}
    for h in hits:
        by_sku.setdefault(h.sku, h)
    for rec in records_for(db, manufacturer, model, serial):
        hit = by_sku.get(rec.part_number)
        if hit is None:
            if rec.kit is None:
                continue        # part number not in the catalog; nothing to show
            hit = FitHit(kit=rec.kit, fitment=None, verdict=SUPPLIED, reason="")
            hits.append(hit)
            by_sku[rec.part_number] = hit
        hit.records.append(rec)
        hit.verdict = SUPPLIED
        where = f" at {rec.customer}" if rec.customer else ""
        hit.reason = (f"this exact truck was supplied {rec.part_number}"
                      f"{where} -- recorded by order management")

    hits.sort(key=lambda h: (_ORDER[h.verdict], h.kit.sku))
    return hits


def confirmed(hits: list[FitHit]) -> list[FitHit]:
    """Hits a salesperson can quote: a rule confirmed it, or it was supplied."""
    return [h for h in hits if h.verdict in (SUPPLIED, CONFIRMED)]


def kits_by_manufacturer(db: Session) -> dict[str, list[Kit]]:
    """Kits grouped by the OEM of the truck they fit -- the default kit-list
    sort. A kit fitting several OEMs appears under every one of them, which is
    the point: a salesperson arrives knowing the truck's brand, not the kit's.
    """
    out: dict[str, list[Kit]] = {}
    seen: set[tuple[str, int]] = set()
    rows = (
        db.query(KitFitment)
        .join(Kit, KitFitment.kit_id == Kit.id)
        .order_by(KitFitment.manufacturer, Kit.sku)
        .all()
    )
    for fit in rows:
        key = (fit.manufacturer, fit.kit_id)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(fit.manufacturer, []).append(fit.kit)
    return out
