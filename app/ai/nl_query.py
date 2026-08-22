"""Plain-English -> SQL over the forklift database, via Claude.

Safety model: Claude only ever *proposes* SQL. Before anything touches the
database, `validate_select` enforces that the statement is a single read-only
SELECT against the allowed table. A per-query LIMIT is appended. The query is
also executed on a connection opened in SQLite read-only mode as defense in
depth, so even a validator miss cannot mutate data.
"""
from __future__ import annotations

import re

from anthropic import Anthropic
from sqlalchemy import text

from ..config import settings
from ..database import engine
from ..schemas import NLQueryResult

MAX_ROWS = 200

# Columns Claude is allowed to reference — doubles as the schema we hand it.
SCHEMA = """Table: forklifts
Columns:
  id INTEGER
  manufacturer TEXT          -- the OEM / brand (Toyota, Crown, Hyster, ...)
  series TEXT                -- product series/family (NULL for one-off models)
  model TEXT
  year_start INTEGER         -- first production year of the model (may be NULL)
  year_end INTEGER           -- last production year; NULL can mean still in production
  internal_serial TEXT       -- serial number assigned by the company (unique)
  capacity_kg REAL           -- rated load capacity, kilograms
  fuel_type TEXT             -- electric | LPG | diesel | gasoline
  chassis TEXT               -- shared-chassis grouping/frame (may be NULL)
  info_status TEXT           -- data review status: green | yellow | red
  source_url TEXT
  pdf_url TEXT
  notes TEXT
  fetched_at DATETIME
  created_at DATETIME

Table: kits                  -- products the company sells for trucks/forklifts
Columns:
  id INTEGER
  sku TEXT                   -- part number, the kit's identity (e.g. 174-CSC52-00G05)
  name TEXT                  -- optional kit name (e.g. Apollo); may be NULL
  price REAL                 -- USD
  notes TEXT
  created_at DATETIME

Table: kit_forklift          -- many-to-many link: which kits fit which forklifts
Columns:
  kit_id INTEGER             -- FK -> kits.id
  forklift_id INTEGER        -- FK -> forklifts.id

A kit fits many forklifts and a forklift is fit by many kits. To connect them,
JOIN through kit_forklift. This works in BOTH directions.

Example — kits that fit forklift model 'SP3500':
  SELECT DISTINCT k.* FROM kits k
  JOIN kit_forklift kf ON kf.kit_id = k.id
  JOIN forklifts f ON f.id = kf.forklift_id
  WHERE LOWER(f.model) LIKE LOWER('%SP3500%');

Kit SKUs sometimes contain stray spaces (e.g. '174- BC20S-00G05'). ALWAYS match a
SKU ignoring spaces and case, using REPLACE to strip spaces from BOTH sides.
Example — forklifts that a kit fits, by SKU '174-BC20S-00G05':
  SELECT DISTINCT f.* FROM forklifts f
  JOIN kit_forklift kf ON kf.forklift_id = f.id
  JOIN kits k ON k.id = kf.kit_id
  WHERE REPLACE(LOWER(k.sku),' ','') LIKE REPLACE(LOWER('%174-BC20S-00G05%'),' ','');"""

_SYSTEM = f"""You translate a user's plain-English question into a single
SQLite SELECT query over this schema:

{SCHEMA}

Rules:
- Output ONLY the SQL, nothing else. No markdown fences, no explanation.
- Exactly one statement, and it MUST be a SELECT (read-only).
- Never use INSERT, UPDATE, DELETE, DROP, ALTER, ATTACH, PRAGMA, or multiple statements.
- Use case-insensitive matching for text (e.g. LOWER(manufacturer) LIKE LOWER('%toyota%')).
- If the question is unanswerable from this schema, return: SELECT 'unanswerable' AS note;"""

# Note: `replace` is intentionally NOT here. The REPLACE *statement* (a write) is
# already blocked by the SELECT/WITH-only + single-statement guards, and we need
# the harmless scalar REPLACE() function for space-insensitive text matching.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|"
    r"truncate|grant|revoke|vacuum)\b",
    re.IGNORECASE,
)


class UnsafeQueryError(ValueError):
    pass


def _extract_sql(raw: str) -> str:
    """Pull the first SQL statement out of a model response.

    Tolerates markdown fences, leading prose ("Here is the query:"),
    SQL comments, and trailing explanations after the semicolon.
    """
    s = raw.strip()
    # Strip markdown code fences.
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"```\s*$", "", s.strip())
    # Drop SQL line comments (full-line and trailing).
    s = re.sub(r"--[^\n]*", "", s)
    # Start at the first SELECT/WITH.
    m = re.search(r"\b(select|with)\b", s, re.IGNORECASE)
    if m:
        s = s[m.start():]
    # Keep only the first statement.
    semi = s.find(";")
    if semi != -1:
        s = s[:semi]
    return s.strip()


def validate_select(sql: str) -> str:
    """Return a sanitized single-SELECT statement or raise UnsafeQueryError."""
    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise UnsafeQueryError("Empty query.")
    if ";" in cleaned:
        raise UnsafeQueryError("Multiple statements are not allowed.")
    if not re.match(r"^(select|with)\b", cleaned, re.IGNORECASE):
        raise UnsafeQueryError("Only SELECT queries are allowed.")
    if _FORBIDDEN.search(cleaned):
        raise UnsafeQueryError("Query contains a disallowed keyword.")
    if "--" in cleaned or "/*" in cleaned:
        raise UnsafeQueryError("SQL comments are not allowed.")
    # Enforce a row cap.
    if not re.search(r"\blimit\b", cleaned, re.IGNORECASE):
        cleaned = f"{cleaned} LIMIT {MAX_ROWS}"
    return cleaned


def _client() -> Anthropic:
    if not settings.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=settings.anthropic_api_key)


def _generate_sql(question: str) -> str:
    client = _client()
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=512,
        system=_SYSTEM,
        messages=[{"role": "user", "content": question}],
    )
    parts = [b.text for b in resp.content if b.type == "text"]
    return _extract_sql("".join(parts))


def _summarize(question: str, columns: list[str], rows: list[list]) -> str:
    """Ask Claude to phrase the result set as a short plain-English answer."""
    client = _client()
    preview = [dict(zip(columns, r)) for r in rows[:30]]
    resp = client.messages.create(
        model=settings.claude_model,
        max_tokens=400,
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Query returned {len(rows)} row(s). Sample:\n{preview}\n\n"
                "Answer the question in 1-3 plain sentences. If zero rows, say "
                "no matching forklifts were found."
            ),
        }],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def ask(question: str) -> NLQueryResult:
    """Full pipeline: question -> validated SQL -> rows -> plain-English answer."""
    result = NLQueryResult(question=question)
    try:
        raw_sql = _generate_sql(question)
        safe_sql = validate_select(raw_sql)
        result.sql = safe_sql
    except UnsafeQueryError as e:
        result.error = f"Rejected the generated query: {e}"
        return result
    except Exception as e:  # noqa: BLE001 - surface API/config errors to the UI
        result.error = f"Could not generate a query: {e}"
        return result

    try:
        # Defense in depth: read-only execution. The pragma MUST be reset
        # before the connection returns to the pool, or later writes
        # (saves/deletes) that reuse this connection will fail.
        is_sqlite = settings.database_url.startswith("sqlite")
        with engine.connect() as conn:
            if is_sqlite:
                conn.exec_driver_sql("PRAGMA query_only = ON")
            try:
                cursor = conn.execute(text(safe_sql))
                result.columns = list(cursor.keys())
                result.rows = [list(row) for row in cursor.fetchall()]
            finally:
                if is_sqlite:
                    conn.exec_driver_sql("PRAGMA query_only = OFF")
    except Exception as e:  # noqa: BLE001
        result.error = f"Query failed: {e}"
        return result

    try:
        result.answer = _summarize(question, result.columns, result.rows)
    except Exception:  # noqa: BLE001 - summary is best-effort
        result.answer = f"Found {len(result.rows)} matching forklift(s)."
    return result
