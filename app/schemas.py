"""Pydantic schemas shared between the AI layer and the API."""
from pydantic import BaseModel, Field


class ForkliftSpecs(BaseModel):
    """Structured specs extracted from a spec sheet. All fields optional."""
    manufacturer: str | None = Field(None, description="OEM / brand")
    series: str | None = Field(None, description="Manufacturer's model-family/range designation this model belongs to, verbatim (e.g. 'E80-120XN', 'FC 5700 series', '8-Series'). Always fill unless truly indeterminable.")
    model: str | None = None
    year_start: int | None = Field(None, description="First production year of this model. Leave null if not confidently known — do not guess.")
    year_end: int | None = Field(None, description="Last production year of this model; null if still in production. Leave null if unknown — do not guess.")
    capacity_kg: float | None = Field(None, description="Rated load capacity in kilograms")
    fuel_type: str | None = Field(None, description="electric, LPG, diesel, or gasoline")
    chassis: str | None = Field(None, description="Shared-chassis grouping/frame this model uses, if stated (e.g. 'Large-Capacity Frame')")
    notes: str | None = None


class LookupResult(BaseModel):
    """What a web lookup returns before the user confirms saving."""
    found: bool
    already_in_db: bool = False
    existing_id: int | None = None
    specs: ForkliftSpecs | None = None
    source_url: str | None = None
    pdf_url: str | None = None
    message: str = ""


class NLQueryResult(BaseModel):
    """Result of a plain-English question against the database."""
    question: str
    sql: str | None = None
    columns: list[str] = []
    rows: list[list] = []
    answer: str = ""
    error: str | None = None
