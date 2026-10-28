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
from functools import lru_cache

from sqlalchemy.orm import Session

from . import kit_ranges, serials
from .models import Kit, KitFitment

CONFIRMED = "confirmed"
EXCLUDED = "excluded"
UNCONFIRMED = "unconfirmed"

_ORDER = {CONFIRMED: 0, UNCONFIRMED: 1, EXCLUDED: 2}


@dataclass
class FitHit:
    """One kit_fitment rule weighed against a truck."""
    kit: Kit
    fitment: KitFitment
    verdict: str
    reason: str

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
        return CONFIRMED, f"serial is within {fit.serial_display}"
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


def kits_for(db: Session, manufacturer: str, model: str,
             serial: str | None = None) -> list[FitHit]:
    """Every kit with a rule naming this truck, confirmed hits first.

    An empty list means no rule names the model at all -- which is different
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

    hits.sort(key=lambda h: (_ORDER[h.verdict], h.kit.sku))
    return hits


def confirmed(hits: list[FitHit]) -> list[FitHit]:
    return [h for h in hits if h.verdict == CONFIRMED]


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
