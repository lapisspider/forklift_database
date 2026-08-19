"""Thin wrapper over Tavily search + extract, tuned for forklift spec sheets."""
from __future__ import annotations

from tavily import TavilyClient

from ..config import settings


def _client() -> TavilyClient:
    if not settings.tavily_api_key:
        raise RuntimeError("TAVILY_API_KEY is not set")
    return TavilyClient(api_key=settings.tavily_api_key)


def search_spec_sheet(query: str, max_results: int = 5) -> dict:
    """Search the web for a forklift spec sheet.

    Returns Tavily's raw response: a dict with 'results' (each having
    'url', 'title', 'content') and possibly 'answer'. We bias the query
    toward manufacturer specification documents.
    """
    client = _client()
    full_query = f"{query} forklift specifications spec sheet capacity lift height datasheet"
    return client.search(
        query=full_query,
        search_depth="advanced",
        max_results=max_results,
        include_raw_content=True,
    )


def answer_search(query: str, max_results: int = 5) -> dict:
    """Search with Tavily's AI-synthesized answer enabled (its analog of a
    Google AI-overview summary). The response 'answer' field often states facts
    like production years that aren't cleanly on any single spec page.
    Returns the raw response ('answer' + 'results')."""
    client = _client()
    return client.search(
        query=query,
        search_depth="advanced",
        max_results=max_results,
        include_answer="advanced",
        include_raw_content=True,
    )


def extract_url(url: str) -> str:
    """Pull the readable text content of a single page/PDF via Tavily."""
    client = _client()
    resp = client.extract(urls=[url])
    results = resp.get("results", [])
    if results:
        return results[0].get("raw_content", "") or ""
    return ""


def best_pdf_url(search_response: dict) -> str | None:
    """Pick the most likely direct PDF link from a search response."""
    for r in search_response.get("results", []):
        url = r.get("url", "")
        if url.lower().endswith(".pdf") or ".pdf" in url.lower():
            return url
    return None
