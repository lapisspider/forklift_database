"""Reading order management's serials into `serial_record`.

The pipeline is: order management knows the serial and the part number it
shipped, and that becomes a confirmed fact about which kit fits which truck.
This module is the door that fact comes through -- used by the CSV importer
(`import_serials.py`) now and by a web form later, which is why the parsing and
the validation live here rather than in the script.

Two rules shape it.

**Nothing is rejected for being unrecognised.** A part number not in the catalog
and a serial that cannot be placed in its OEM's scheme are both still recorded,
with `kit_id` or `serial_seq` left null. The record is the fact; our ability to
interpret it is not, and discarding it loses the only copy.

**Re-importing the same export must add nothing.** Order management will send
overlapping files, so identity is checked before every insert. `order_ref` plus
part number plus serial where there is an order reference; part number, OEM,
model and serial where there is not.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

from sqlalchemy.orm import Session

from . import serials
from .models import Kit, SerialRecord

# Header spellings seen in the wild, lower-cased with spaces and punctuation
# stripped. The left side is the field; everything on the right maps onto it.
_ALIASES: dict[str, tuple[str, ...]] = {
    "part_number": ("partnumber", "part", "partno", "partn", "sku", "kit",
                    "kitpartnumber", "ssc", "sscpart"),
    "manufacturer": ("manufacturer", "oem", "make", "brand", "truckmake",
                     "truckmanufacturer", "truckoem"),
    "model": ("model", "modelnumber", "modelno", "truckmodel", "unitmodel"),
    "serial": ("serial", "serialnumber", "serialno", "sn", "truckserial",
               "unitserial", "serial#"),
    "customer": ("customer", "customername", "account", "enduser", "company"),
    "location": ("location", "site", "city", "customerlocation", "shipto",
                 "address"),
    "order_ref": ("orderref", "order", "ordernumber", "orderno", "so",
                  "salesorder", "ordernum", "po", "ponumber"),
    "note": ("note", "notes", "comment", "comments", "remarks"),
}
_REQUIRED = ("part_number", "manufacturer", "model", "serial")


def _key(header: str) -> str:
    return "".join(ch for ch in header.lower() if ch.isalnum() or ch == "#")


def map_headers(headers: list[str]) -> tuple[dict[str, str], list[str]]:
    """Map a CSV's headers onto field names. Returns (mapping, unmapped)."""
    lookup = {alias: fieldname
              for fieldname, aliases in _ALIASES.items()
              for alias in aliases}
    mapping: dict[str, str] = {}
    unmapped: list[str] = []
    for h in headers:
        fieldname = lookup.get(_key(h))
        if fieldname and fieldname not in mapping.values():
            mapping[h] = fieldname
        else:
            unmapped.append(h)
    return mapping, unmapped


@dataclass
class IntakeRow:
    """One row read and judged, before anything is written."""
    line: int
    data: dict[str, str]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    kit_id: int | None = None
    duplicate_of: int | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def writable(self) -> bool:
        return self.ok and self.duplicate_of is None


@dataclass
class IntakeReport:
    rows: list[IntakeRow] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    @property
    def writable(self) -> list[IntakeRow]:
        return [r for r in self.rows if r.writable]

    @property
    def failed(self) -> list[IntakeRow]:
        return [r for r in self.rows if not r.ok]

    @property
    def duplicates(self) -> list[IntakeRow]:
        return [r for r in self.rows if r.ok and r.duplicate_of is not None]


def _find_duplicate(db: Session, data: dict[str, str]) -> int | None:
    """The id of a record already holding this fact, if there is one.

    Identity is the order reference where one exists, because that is what order
    management treats as the unit. Where it does not, the truck plus the part
    number is the next best thing -- the same kit shipped to the same serial
    twice is far more likely to be a re-export than a second shipment.
    """
    q = db.query(SerialRecord.id).filter(
        SerialRecord.part_number == data["part_number"],
        SerialRecord.serial == data["serial"],
    )
    if data.get("order_ref"):
        q = q.filter(SerialRecord.order_ref == data["order_ref"])
    else:
        q = q.filter(SerialRecord.manufacturer == data["manufacturer"],
                     SerialRecord.model == data["model"])
    row = q.first()
    return row[0] if row else None


def judge(db: Session, line: int, raw: dict[str, str],
          seen: dict[tuple, int] | None = None) -> IntakeRow:
    """Validate and resolve one row. Writes nothing."""
    data = {k: (v or "").strip() for k, v in raw.items()}
    row = IntakeRow(line=line, data=data)

    for field_name in _REQUIRED:
        if not data.get(field_name):
            row.errors.append(f"{field_name} is empty")
    if row.errors:
        return row

    kit = db.query(Kit).filter(Kit.sku == data["part_number"]).first()
    if kit is None:
        # Not an error: the fact is still worth keeping, and a part number can
        # reach us before it reaches the catalog.
        row.warnings.append(
            f"part number {data['part_number']} is not in the kit catalog; "
            f"recorded with no kit link")
    else:
        row.kit_id = kit.id

    point = serials.serial_point(data["manufacturer"], data["serial"])
    if point is None:
        row.warnings.append(
            f"serial cannot be placed: {serials.explain(data['manufacturer'])}")

    existing = _find_duplicate(db, data)
    if existing is not None:
        row.duplicate_of = existing
    elif seen is not None:
        # Same file repeating itself, before anything is committed.
        key = (data["part_number"], data["serial"],
               data.get("order_ref") or (data["manufacturer"], data["model"]))
        if key in seen:
            row.duplicate_of = -seen[key]      # negative = earlier line, not a row id
        else:
            seen[key] = line
    return row


def read_csv(db: Session, text: str) -> IntakeReport:
    """Parse and judge a whole CSV. Writes nothing."""
    reader = csv.DictReader(StringIO(text))
    report = IntakeReport()
    headers = reader.fieldnames or []
    report.mapping, report.unmapped = map_headers(headers)
    present = set(report.mapping.values())
    report.missing_fields = [f for f in _REQUIRED if f not in present]
    if report.missing_fields:
        return report

    seen: dict[tuple, int] = {}
    for i, raw in enumerate(reader, start=2):      # line 1 is the header
        mapped = {report.mapping[h]: v for h, v in raw.items()
                  if h in report.mapping}
        report.rows.append(judge(db, i, mapped, seen))
    return report


def read_csv_file(db: Session, path: str | Path) -> IntakeReport:
    return read_csv(db, Path(path).read_text(encoding="utf-8-sig"))


def to_record(row: IntakeRow, recorded_by: str | None = None) -> SerialRecord:
    """A judged row as a storable record. Derived fields resolved here so the
    importer and a web form cannot disagree about them."""
    data = row.data
    point = serials.serial_point(data["manufacturer"], data["serial"])
    return SerialRecord(
        part_number=data["part_number"],
        manufacturer=data["manufacturer"],
        model=data["model"],
        serial=data["serial"],
        customer=data.get("customer") or None,
        location=data.get("location") or None,
        order_ref=data.get("order_ref") or None,
        kit_id=row.kit_id,
        serial_norm=serials.normalize_serial(data["serial"]) or None,
        serial_seq=point.sequence if point else None,
        serial_scope=point.scope if point else None,
        status="active",
        note=data.get("note") or None,
        recorded_by=recorded_by,
    )


def commit(db: Session, report: IntakeReport,
           recorded_by: str | None = None) -> list[SerialRecord]:
    """Write every writable row. Duplicates and failures are skipped."""
    written = [to_record(r, recorded_by) for r in report.writable]
    db.add_all(written)
    db.commit()
    return written
