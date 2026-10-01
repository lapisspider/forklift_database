"""Database engine, session factory, and read-only connection helper."""
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# Committed reference data for offline Hyster/Yale serial decoding (see
# app/ai/serial_decode.py). Rebuilt into the DB whenever these files change.
_REF_DIR = Path(__file__).resolve().parent.parent / "data" / "reference"
_REF_FILES = [
    "hy_serial_prefixes.json", "hy_plant_codes.json", "hy_year_codes.json",
    "doosan_prefixes.json", "doosan_year_serials.json",
]

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

if _is_sqlite:
    # SQLite ignores foreign-key rules (e.g. ON DELETE SET NULL on kits)
    # unless each connection opts in.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_schema() -> None:
    """SQLite-only: add columns/views introduced after create_all shipped, so an
    existing DB file catches up without a migration tool. Idempotent."""
    if not _is_sqlite:
        return
    with engine.begin() as conn:
        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(forklifts)")]
        if "platform_group" not in cols:
            conn.exec_driver_sql("ALTER TABLE forklifts ADD COLUMN platform_group VARCHAR(40)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_forklifts_platform_group ON forklifts(platform_group)"
        )
        # Kits no longer transfer between twins -- drop the old inherited-fit view
        # (kept as a DROP so existing DBs/the seed get cleaned up on upgrade).
        conn.exec_driver_sql("DROP VIEW IF EXISTS kit_fit_effective")

        _load_reference_data(conn)


def _load_reference_data(conn) -> None:
    """Rebuild the serial_prefixes/serial_plant_codes/serial_year_codes tables
    from committed JSON, guarded by a content hash so it's a no-op unless the
    JSON changed. Means deploy/seed.db doesn't need to carry this data."""
    conn.exec_driver_sql(
        "CREATE TABLE IF NOT EXISTS serial_ref_meta (key VARCHAR(40) PRIMARY KEY, value TEXT)"
    )
    digest = hashlib.sha256()
    for name in _REF_FILES:
        digest.update((_REF_DIR / name).read_bytes())
    content_hash = digest.hexdigest()

    row = conn.exec_driver_sql(
        "SELECT value FROM serial_ref_meta WHERE key = 'serial_ref_hash'"
    ).fetchone()
    if row and row[0] == content_hash:
        return

    prefixes = json.loads((_REF_DIR / "hy_serial_prefixes.json").read_text(encoding="utf-8"))
    plants = json.loads((_REF_DIR / "hy_plant_codes.json").read_text(encoding="utf-8"))
    years = json.loads((_REF_DIR / "hy_year_codes.json").read_text(encoding="utf-8"))

    conn.exec_driver_sql("DELETE FROM serial_prefixes")
    conn.exec_driver_sql("DELETE FROM serial_plant_codes")
    conn.exec_driver_sql("DELETE FROM serial_year_codes")
    conn.exec_driver_sql("DELETE FROM doosan_prefixes")
    conn.exec_driver_sql("DELETE FROM doosan_year_serials")

    conn.execute(
        text(
            "INSERT INTO serial_prefixes "
            "(prefix, brand, models_raw, ita_class, capacity_text, product_type, twin_prefix, source) "
            "VALUES (:prefix, :brand, :models_raw, :ita_class, :capacity_text, :product_type, :twin_prefix, :source)"
        ),
        [
            {
                "prefix": p["prefix"],
                "brand": p["brand"],
                "models_raw": json.dumps(p["models"]),
                "ita_class": p["ita_class"],
                "capacity_text": p["capacity_text"],
                "product_type": p["product_type"],
                "twin_prefix": p["twin_prefix"],
                "source": p["source"],
            }
            for p in prefixes
        ],
    )
    conn.execute(
        text("INSERT INTO serial_plant_codes (code, location, brands) VALUES (:code, :location, :brands)"),
        plants,
    )
    conn.execute(
        text("INSERT INTO serial_year_codes (code, y1, y2, y3, y4) VALUES (:code, :y1, :y2, :y3, :y4)"),
        [
            {
                "code": code,
                "y1": yrs[0] if len(yrs) > 0 else None,
                "y2": yrs[1] if len(yrs) > 1 else None,
                "y3": yrs[2] if len(yrs) > 2 else None,
                "y4": yrs[3] if len(yrs) > 3 else None,
            }
            for code, yrs in years.items()
        ],
    )
    doosan_prefixes = json.loads((_REF_DIR / "doosan_prefixes.json").read_text(encoding="utf-8"))
    doosan_years = json.loads((_REF_DIR / "doosan_year_serials.json").read_text(encoding="utf-8"))

    conn.execute(
        text(
            "INSERT INTO doosan_prefixes "
            "(prefix, power, model, engine, certification, fuel_trans, brake, voltage, system, configuration) "
            "VALUES (:prefix, :power, :model, :engine, :certification, :fuel_trans, :brake, :voltage, :system, :configuration)"
        ),
        [
            {
                "prefix": r["prefix"],
                "power": r.get("power") or None,
                "model": r["model"],
                "engine": r.get("engine") or None,
                "certification": r.get("certification") or None,
                "fuel_trans": r.get("fuel_trans") or None,
                "brake": r.get("brake") or None,
                "voltage": r.get("voltage") or None,
                "system": r.get("system") or None,
                "configuration": r.get("configuration") or None,
            }
            for r in doosan_prefixes
        ],
    )
    conn.execute(
        text(
            "INSERT INTO doosan_year_serials (model, year, prefix, sequence, sequence_raw, raw, flag) "
            "VALUES (:model, :year, :prefix, :sequence, :sequence_raw, :raw, :flag)"
        ),
        [
            {
                "model": r["model"],
                "year": r["year"],
                "prefix": r["prefix"],
                "sequence": r["sequence"],
                "sequence_raw": r["sequence_raw"],
                "raw": r["raw"],
                "flag": r.get("flag") or None,
            }
            for r in doosan_years
        ],
    )

    conn.execute(
        text(
            "INSERT INTO serial_ref_meta (key, value) VALUES ('serial_ref_hash', :h) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        ),
        {"h": content_hash},
    )


def init_db() -> None:
    """Create tables if they don't exist yet."""
    from . import models  # noqa: F401  (registers mapped classes)
    Base.metadata.create_all(bind=engine)
    _ensure_schema()
