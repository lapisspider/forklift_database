"""SQLAlchemy models for the forklift database."""
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Table, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Many-to-many link: a kit fits many forklifts; a forklift is fit by many kits.
# ON DELETE CASCADE removes the link rows (not the kit/forklift) when either
# side is deleted — FK enforcement is enabled in database.py.
kit_forklift = Table(
    "kit_forklift",
    Base.metadata,
    Column("kit_id", ForeignKey("kits.id", ondelete="CASCADE"), primary_key=True),
    Column("forklift_id", ForeignKey("forklifts.id", ondelete="CASCADE"), primary_key=True),
)


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

    # Provenance
    source_url: Mapped[str | None] = mapped_column(Text)             # where the spec sheet lives
    pdf_url: Mapped[str | None] = mapped_column(Text)               # direct link to the PDF, if found
    pdf_path: Mapped[str | None] = mapped_column(Text)             # local archived copy, if saved
    notes: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Kits that fit this forklift (many-to-many).
    kits: Mapped[list["Kit"]] = relationship(
        secondary=kit_forklift, back_populates="forklifts"
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
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # 'admin' | 'viewer'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


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

    # Forklifts this kit fits (many-to-many).
    forklifts: Mapped[list["Forklift"]] = relationship(
        secondary=kit_forklift, back_populates="kits"
    )

    def as_dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
