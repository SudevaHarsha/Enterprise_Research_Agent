"""Built-in mock search backend for local demo / keyless evaluation.

Returns ``file://`` URIs pointing at the ``sample_data/`` directory so the
pipeline can exercise the full collect → store → extract → verify flow
without a live search API.
"""

from __future__ import annotations

import os
from pathlib import Path

# Resolve sample_data relative to the project root (WORKDIR in Docker = /app),
# not relative to this source file (which may live in a temp copy).
_SAMPLE_DATA_DIR = Path(os.environ.get("ECRKE_DATA_DIR", "/app")) / "sample_data"


def _load_seed_uris() -> list[str]:
    """Return file:// URIs for every sample file that exists."""
    uris: list[str] = []
    for name in [
        "retail_operations_report.html",
        "retail_operations_report.txt",
        "retail_operations_feed.rss",
        "ecrke_seed_report.pdf",
    ]:
        p = _SAMPLE_DATA_DIR / name
        if p.exists():
            uris.append(p.as_uri())
    return uris


_SEED_URIS = _load_seed_uris()


async def mock_search(query: str, limit: int) -> list[str]:  # noqa: ARG001
    """Return sample-data URIs for any query (deterministic, zero-network)."""
    return _SEED_URIS[:limit]
