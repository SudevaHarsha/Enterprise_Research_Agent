"""RSS/Atom feed parsing to candidate URLs (stdlib ``xml.etree`` — no feedparser).

Only the candidate-extraction layer lives here; fetching candidates goes
through the allowlist-gated Fetcher/URLConnector so G-06 still applies to every
outbound request. Feeds are treated as untrusted input: a non-well-formed feed
raises :class:`RSSParseError` and is never partially consumed.
"""

from __future__ import annotations

# Feeds are untrusted; stdlib ET does not resolve external entities and the
# input is length-capped before parsing. defusedxml is intentionally not added:
# the collector stays stdlib-only per the task brief.
import xml.etree.ElementTree as ET  # noqa: S314  # nosec B405

from app.core.logging import get_logger

logger = get_logger(__name__)

# Feeds are untrusted and length-capped before parsing (memory DoS guard).
_MAX_FEED_BYTES = 5 * 1024 * 1024  # 5 MiB


class RSSParseError(ValueError):
    """Raised when feed XML cannot be parsed or has an unrecognized root."""


class RSSConnector:
    """Parse RSS 2.0 / Atom feeds into candidate source URIs."""

    def fetch_candidates(self, feed_xml: bytes, base_feed_url: str) -> list[str]:
        """Return item/entry link URIs from an RSS or Atom feed document."""
        if len(feed_xml) > _MAX_FEED_BYTES:
            raise RSSParseError(
                f"feed from {base_feed_url} exceeds {_MAX_FEED_BYTES} byte cap "
                "(rejected before parsing)"
            )
        try:
            root = ET.fromstring(feed_xml)  # noqa: S314  # nosec B314
        except ET.ParseError as exc:
            raise RSSParseError(f"invalid feed XML from {base_feed_url}: {exc}") from exc
        root_name = _local_name(root.tag)
        if root_name == "rss":
            return _rss_candidates(root)
        if root_name == "feed":
            return _atom_candidates(root)
        raise RSSParseError(f"unrecognized feed root <{root_name}> from {base_feed_url}")


def _local_name(tag: str) -> str:
    """Strip an XML namespace prefix from a tag name (``{ns}item`` -> ``item``)."""
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _child_text(element: ET.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def _rss_candidates(root: ET.Element) -> list[str]:
    candidates: list[str] = []
    for item in root.iter():
        if _local_name(item.tag) == "item":
            link = _child_text(item, "link")
            if link:
                candidates.append(link)
    return candidates


def _atom_candidates(root: ET.Element) -> list[str]:
    candidates: list[str] = []
    for entry in root.iter():
        if _local_name(entry.tag) == "entry":
            best: str | None = None
            for child in entry:
                if _local_name(child.tag) == "link":
                    rel = child.attrib.get("rel", "alternate")
                    href = (child.attrib.get("href") or "").strip()
                    if href and (best is None or rel == "alternate"):
                        best = href
            if best:
                candidates.append(best)
    return candidates
