"""Egress allowlist — G-06 default-deny gate.

The egress sandbox enforces that every outbound fetch URI points at a host that
is (a suffix of) a domain named in ``ALLOWED_DOMAINS`` (comma-separated). The
gate is applied *before* any network I/O — a denied URI is refused even before
the transport layer is touched.

The allowlist only ever handles URIs and hostnames; it never sees credentials.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlparse

from app.core.config import Settings


class AllowlistDeniedError(PermissionError):
    """Raised when a URI's host is not on the egress allowlist (G-06)."""


class Allowlist:
    """Default-deny domain suffix allowlist for outbound egress (G-06)."""

    def __init__(self, domains: Iterable[str]) -> None:
        self._domains: tuple[str, ...] = tuple(
            sorted(
                {
                    domain.strip().lower().lstrip("*.")
                    for domain in domains
                    if domain and domain.strip()
                }
            )
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Allowlist:
        """Build an allowlist from the comma-separated ``ALLOWED_DOMAINS`` setting."""
        raw = settings.allowed_domains or ""
        domains = [part.strip() for part in raw.split(",") if part.strip()]
        return cls(domains)

    def allows(self, host: str) -> bool:
        """Return True when ``host`` is an allowlisted domain or one of its subdomains."""
        host = host.strip().lower()
        if not host:
            return False
        return any(host == domain or host.endswith("." + domain) for domain in self._domains)

    def check(self, uri: str) -> str:
        """Return the URI when its host is allowlisted; otherwise raise (G-06)."""
        parsed = urlparse(uri)
        host = (parsed.hostname or "").lower()
        if not host or not self.allows(host):
            raise AllowlistDeniedError(
                f"URI host {host or '(none)'!r} is not on the ALLOWED_DOMAINS "
                "egress allowlist (G-06)"
            )
        return uri
