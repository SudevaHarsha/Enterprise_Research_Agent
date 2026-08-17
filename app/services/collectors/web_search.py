"""DuckDuckGo web search collector — free, no API key required.

Uses the ``ddgs`` package to return candidate URIs for a research query.
Results are URL-only (the fetcher handles content retrieval).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def web_search(query: str, limit: int = 10) -> list[str]:
    """Search DuckDuckGo for *query* and return up to *limit* result URLs."""
    try:
        from ddgs import DDGS

        results = DDGS().text(query, max_results=limit)
        urls = [r["href"] for r in results if r.get("href")]
        logger.info("web_search: %d results for %r", len(urls), query)
        return urls
    except Exception:
        logger.exception("web_search failed for %r", query)
        return []
