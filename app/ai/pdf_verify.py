"""Confirm a candidate spec-sheet URL is a live PDF before it is ever returned."""
from __future__ import annotations

from urllib.parse import urlparse

import httpx

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; forklift-db link check)"}
_TIMEOUT = httpx.Timeout(8.0)


def _is_pdf(url: str, content_type: str, head: bytes = b"") -> bool:
    ctype = (content_type or "").lower()
    if "pdf" in ctype or head.startswith(b"%PDF"):
        return True
    return urlparse(url).path.lower().endswith(".pdf") and "html" not in ctype


def verify_pdf_url(url: str) -> int | None:
    """HTTP status if `url` is a reachable PDF (2xx and PDF-ish), else None.
    HEAD first; a ranged GET covers servers that reject or botch HEAD."""
    try:
        with httpx.Client(follow_redirects=True, timeout=_TIMEOUT, headers=_HEADERS) as client:
            r = client.head(url)
            if 200 <= r.status_code < 300 and _is_pdf(str(r.url), r.headers.get("content-type", "")):
                return r.status_code
            with client.stream("GET", url, headers={"Range": "bytes=0-1023"}) as g:
                if not 200 <= g.status_code < 300:
                    return None
                head = next(g.iter_bytes(1024), b"")
                return g.status_code if _is_pdf(str(g.url), g.headers.get("content-type", ""), head) else None
    except httpx.HTTPError:
        return None
