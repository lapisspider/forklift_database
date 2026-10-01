"""SQLAlchemy models for the forklift database."""
import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# Connection-tier ranking (gold best) for computing a forklift's "best" tier.
TIER_RANK = {"gold": 3, "silver": 2, "bronze": 1}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Forklift(Base):
    __tablename__ = "forklifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Identity. manufacturer == OEM (required); series optional.
    manufacturer: Mapped[str] = mapped_column(String(120), index=True)
    series: Mapped[str | None] = mapped_column(String(120), index=True)
    model: Mapped[str] = mapped_column(String(200), index=True)

    # Company-assigned serial (unique) — kits reference the forklift by id.
    internal_serial: Mapped[str | None] = mapped_column(String(120), unique=True, index=True)

    # Production-year span of the model. year_end NULL = still in production.
    year_start: Mapped[int | None] = mapped_column(Integer, index=True)
    year_end: Mapped[int | None] = mapped_column(Integer)

    # Core specs (all optional — the web may not surface every field)
    capacity_kg: Mapped[float | None] = mapped_column(Float)          # rated load capacity
    fuel_type: Mapped[str | None] = mapped_column(String(40))         # electric / LPG / diesel / gas
    chassis: Mapped[str | None] = mapped_column(String(80))           # shared-chassis grouping/frame
    truck_class: Mapped[str | None] = mapped_column(String(20), index=True)  # OSHA class, e.g. "Class V"
    platform_group: Mapped[str | None] = mapped_column(String(40), index=True)  # shared-platform key (Hyster/Yale twins)

    # Review status of this forklift's data: green|yellow|red (set manually).
    info_status: Mapped[str] = mapped_column(String(10), default="yellow", index=True)

    # Provenance
    source_url: Mapped[str | None] = mapped_column(Text)             # where the spec sheet lives
    pdf_url: Mapped[str | None] = mapped_column(Text)               # direct link to the PDF, if found
    pdf_path: Mapped[str | None] = mapped_column(Text)             # local archived copy, if saved
    notes: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Kits that fit this forklift. kit_links are the rich connection rows (with
    # tier/sign-off); `kits` is a convenience proxy to the Kit objects.
    kit_links: Mapped[list["KitForklift"]] = relationship(
        back_populates="forklift", cascade="all, delete-orphan"
    )
    kits = association_proxy("kit_links", "kit",
                             creator=lambda k: KitForklift(kit=k))

    @property
    def best_tier(self) -> str | None:
        """Highest connection tier across this forklift's kits (gold>silver>bronze)."""
        tiers = [l.tier for l in self.kit_links]
        return max(tiers, key=lambda t: TIER_RANK.get(t, 0)) if tiers else None

    def twins(self, db) -> list["Forklift"]:
        """Same platform_group, different manufacturer (never same-brand fuel variants)."""
        if not self.platform_group:
            return []
        return (
            db.query(Forklift)
            .filter(Forklift.platform_group == self.platform_group,
                    Forklift.manufacturer != self.manufacturer)
            .order_by(Forklift.manufacturer, Forklift.model)
            .all()
        )

    @property
    def year_display(self) -> str:
        """Human-friendly production span: '2012', '2005–2015', '2018–present', '—'."""
        s, e = self.year_start, self.year_end
        if s and e:
            return str(s) if s == e else f"{s}–{e}"
        if s:
            return f"{s}–present"
        if e:
            return f"–{e}"
        return "—"

    def as_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class User(Base):
    """An employee who can sign in. Role gates editing (admin vs viewer)."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # 'admin' | 'editor' | 'viewer'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_edit(self) -> bool:
        return self.role in ("admin", "editor")


class Kit(Base):
    """A product/kit the company sells, identified by its SKU (part number).

    A kit fits one or more forklift models (many-to-many via kit_forklift)."""
    __tablename__ = "kits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(120), index=True)       # part number, required
    name: Mapped[str | None] = mapped_column(String(200), index=True)  # optional (Apollo, ...)
    price: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Forklifts this kit fits. forklift_links are the rich connection rows;
    # `forklifts` is a convenience proxy to the Forklift objects.
    forklift_links: Mapped[list["KitForklift"]] = relationship(
        back_populates="kit", cascade="all, delete-orphan"
    )
    forklifts = association_proxy("forklift_links", "forklift",
                                  creator=lambda f: KitForklift(forklift=f))

    def as_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class KitForklift(Base):
    """One kit↔forklift compatibility connection, with its validation tier.

    tier: bronze (editor-reviewed, initial) | silver (admin reviewed) |
    gold (admin + install confirmation: technician_name + install_date)."""
    __tablename__ = "kit_forklift"

    kit_id: Mapped[int] = mapped_column(
        ForeignKey("kits.id", ondelete="CASCADE"), primary_key=True)
    forklift_id: Mapped[int] = mapped_column(
        ForeignKey("forklifts.id", ondelete="CASCADE"), primary_key=True)
    tier: Mapped[str] = mapped_column(String(10), default="bronze")
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    technician_name: Mapped[str | None] = mapped_column(String(255))   # for gold
    install_date: Mapped[str | None] = mapped_column(String(20))       # ISO date, for gold
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    kit: Mapped["Kit"] = relationship(back_populates="forklift_links")
    forklift: Mapped["Forklift"] = relationship(back_populates="kit_links")


class SerialPrefix(Base):
    """Chars 1-4 of a Hyster/Yale OEM serial: design generation + model family.

    No FK to forklifts -- a prefix names a model family, resolved at query time
    (app/ai/serial_decode.py). A prefix can repeat across rows (regional/sub-model
    variants, or one row per source), so duplicates are kept, not merged."""
    __tablename__ = "serial_prefixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prefix: Mapped[str] = mapped_column(String(4), index=True)
    brand: Mapped[str] = mapped_column(String(20))
    models_raw: Mapped[str] = mapped_column(Text)     # JSON list of model designators
    ita_class: Mapped[str | None] = mapped_column(String(10))
    capacity_text: Mapped[str | None] = mapped_column(String(60))
    product_type: Mapped[str | None] = mapped_column(String(120))
    twin_prefix: Mapped[str | None] = mapped_column(String(4))
    source: Mapped[str] = mapped_column(String(40))

    @property
    def models(self) -> list[str]:
        return json.loads(self.models_raw)


class SerialPlantCode(Base):
    """Char 5 of a Hyster/Yale OEM serial: the factory that built the truck."""
    __tablename__ = "serial_plant_codes"

    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    location: Mapped[str] = mapped_column(String(120))
    brands: Mapped[str] = mapped_column(String(40))


class SerialYearCode(Base):
    """Last char of a Hyster/Yale OEM serial: one letter maps to several
    candidate years (the 23-letter alphabet repeats every 23 years)."""
    __tablename__ = "serial_year_codes"

    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    y1: Mapped[int | None] = mapped_column(Integer)
    y2: Mapped[int | None] = mapped_column(Integer)
    y3: Mapped[int | None] = mapped_column(Integer)
    y4: Mapped[int | None] = mapped_column(Integer)

    @property
    def years(self) -> list[int]:
        return [y for y in (self.y1, self.y2, self.y3, self.y4) if y is not None]


class DoosanPrefix(Base):
    """Chars 1-2 (+ trailing dash) of a Doosan/Bobcat serial: power/engine/
    brake/voltage spec for that build. A prefix can legitimately repeat
    (e.g. 'FH-' has two rows) -- duplicates are kept, not merged. No FK to
    forklifts -- resolved against the catalog at query time, same pattern as
    SerialPrefix for Hyster/Yale."""
    __tablename__ = "doosan_prefixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prefix: Mapped[str] = mapped_column(String(4), index=True)   # e.g. "L7-", "FH-", "21-"
    power: Mapped[str | None] = mapped_column(String(20))        # IC | Electric
    model: Mapped[str] = mapped_column(String(120))              # raw model-family text (may be a range)
    engine: Mapped[str | None] = mapped_column(String(80))
    certification: Mapped[str | None] = mapped_column(String(40))
    fuel_trans: Mapped[str | None] = mapped_column(String(40))
    brake: Mapped[str | None] = mapped_column(String(40))
    voltage: Mapped[str | None] = mapped_column(String(20))
    system: Mapped[str | None] = mapped_column(String(20))
    configuration: Mapped[str | None] = mapped_column(String(120))


class DoosanYearSerial(Base):
    """One 'first serial of the model-year' batch marker for a Doosan prefix.
    Sequences are batch-allocated, not calendar counters -- a higher sequence
    can predate a lower one (see app/ai/decoders/doosan.py:estimate_year).
    No FK to forklifts."""
    __tablename__ = "doosan_year_serials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(String(120), index=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    prefix: Mapped[str] = mapped_column(String(4), index=True)
    sequence: Mapped[int] = mapped_column(Integer)        # parsed int, for comparison
    sequence_raw: Mapped[str] = mapped_column(String(10))  # as printed (e.g. "0389" vs "00389")
    raw: Mapped[str] = mapped_column(String(20))           # full marker as printed, e.g. "21-00932"
    flag: Mapped[str | None] = mapped_column(Text)         # source anomaly note, if any
