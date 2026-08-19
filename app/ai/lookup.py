"""Orchestrates: check DB -> web search -> extract -> propose for confirmation."""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Forklift
from ..schemas import ForkliftSpecs, LookupResult
from . import extractor, tavily_client


def find_in_db(db: Session, query: str) -> Forklift | None:
    """Loose match on 'manufacturer model' or just model text."""
    q = query.strip().lower()
    # `+` on string columns renders as portable concatenation (|| in SQLite).
    combined = func.lower(Forklift.manufacturer + " " + Forklift.model)
    return (
        db.query(Forklift)
        .filter(
            func.lower(Forklift.model).like(f"%{q}%")
            | combined.like(f"%{q}%")
        )
        .first()
    )


def lookup(db: Session, query: str) -> LookupResult:
    """Look up a forklift by code/model. DB first, then the web."""
    existing = find_in_db(db, query)
    if existing:
        return LookupResult(
            found=True,
            already_in_db=True,
            existing_id=existing.id,
            specs=ForkliftSpecs(**{
                k: v for k, v in existing.as_dict().items()
                if k in ForkliftSpecs.model_fields
            }),
            source_url=existing.source_url,
            pdf_url=existing.pdf_url,
            message=f"Already in the database (record #{existing.id}).",
        )

    if not settings.web_lookup_enabled:
        return LookupResult(
            found=False,
            message="Not in the database. Web lookup is disabled — add "
                    "ANTHROPIC_API_KEY and TAVILY_API_KEY to .env to enable it.",
        )

    search = tavily_client.search_spec_sheet(query)
    results = search.get("results", [])
    if not results:
        return LookupResult(found=False, message="No spec sheet found online.")

    # Prefer a direct PDF; otherwise use the top result's content.
    pdf_url = tavily_client.best_pdf_url(search)
    top = results[0]
    source_url = top.get("url")
    raw = top.get("raw_content") or top.get("content") or ""
    if pdf_url and not raw:
        raw = tavily_client.extract_url(pdf_url)

    specs = extractor.extract_specs(raw, hint=query)
    if not specs.manufacturer and not specs.model:
        return LookupResult(
            found=False,
            source_url=source_url,
            pdf_url=pdf_url,
            message="Found a page but couldn't extract specs. Check the source link.",
        )

    return LookupResult(
        found=True,
        already_in_db=False,
        specs=specs,
        source_url=source_url,
        pdf_url=pdf_url,
        message="Found online. Review the specs below and confirm to save.",
    )
