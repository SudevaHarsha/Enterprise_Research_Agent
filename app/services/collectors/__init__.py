"""Collection connectors — search, RSS/Atom, direct URL (task_005).

Each connector produces candidate URIs or fully ingested ``Source``/``Passage``
rows through the allowlist-gated, rate-limited, redaction-aware pipeline
(G-04/G-05/G-06). All connectors are deterministic and LLM-free.
"""

from app.services.collectors.rss import RSSConnector, RSSParseError
from app.services.collectors.search import SearchConnector, SearchProviderError
from app.services.collectors.url import URLConnector

__all__ = [
    "RSSConnector",
    "RSSParseError",
    "SearchConnector",
    "SearchProviderError",
    "URLConnector",
]
