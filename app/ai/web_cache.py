"""A cache for paid web results that survives a worker boundary and a restart.

The caches this replaces were module-level dicts. Production runs
`gunicorn -w 2` (entrypoint.sh), so each worker held its own copy and a repeat
question had roughly an even chance of landing on the cold one -- and every
deploy threw both away. The saving is not small: the same cross-market question
measured 19.8 s cold against 4.6 s warm.

Stored in the application database so it is shared by every worker, with an
explicit expiry rather than a dict that only grows.

Every operation is best-effort. A cache that cannot be read or written must
never break a lookup, so failures degrade to "no cache" and stay silent -- the
caller just pays for the search it would have paid for anyway.
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import text

from ..database import engine

DEFAULT_TTL = 600  # 10 minutes, matching the dict caches this replaces
_PRUNE_EVERY = 50  # writes between expiry sweeps
_writes = 0


def _key(namespace: str, raw: str) -> str:
    return f"{namespace}|{' '.join((raw or '').split()).lower()}"


def get(namespace: str, raw: str) -> Any | None:
    """The cached JSON value, or None when absent, expired or unreadable."""
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value, expires_at FROM web_cache WHERE key = :k"),
                {"k": _key(namespace, raw)},
            ).fetchone()
            if row is None or row[1] < time.time():
                return None
            return json.loads(row[0])
    except Exception:  # noqa: BLE001 -- a cache miss is always a safe answer
        return None


def put(namespace: str, raw: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """Store a JSON-serialisable value. Silent on failure."""
    global _writes
    try:
        payload = json.dumps(value)
    except (TypeError, ValueError):
        return
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO web_cache (key, value, expires_at) VALUES (:k, :v, :e) "
                     "ON CONFLICT(key) DO UPDATE SET value = :v, expires_at = :e"),
                {"k": _key(namespace, raw), "v": payload, "e": time.time() + ttl},
            )
            _writes += 1
            if _writes % _PRUNE_EVERY == 0:
                conn.execute(text("DELETE FROM web_cache WHERE expires_at < :now"),
                             {"now": time.time()})
    except Exception:  # noqa: BLE001 -- never let caching break a lookup
        return


def clear() -> None:
    """Drop everything. For tests and for a deliberate refresh."""
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM web_cache"))
    except Exception:  # noqa: BLE001
        return
