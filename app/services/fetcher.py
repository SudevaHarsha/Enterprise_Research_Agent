"""Allowlist-gated, rate-limited HTTP fetching (G-06 egress sandbox).

Every outbound request passes the allowlist gate *before* any I/O, and
per-connector rate limits are enforced with an injectable clock and sleep so
behavior is fully deterministic under test (no real waiting, no real network).

Redirects are never followed (``follow_redirects=False``): a redirect target
could leave the allowlist, so a non-200 response surfaces as :class:`FetchError`
instead of silently escaping the sandbox.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from app.core.config import Settings
from app.services.allowlist import Allowlist


class FetchError(RuntimeError):
    """Raised when a fetch does not produce usable content (non-200 or transport error)."""


@dataclass(frozen=True)
class FetchedContent:
    """Result of a successful allowlisted fetch."""

    uri: str
    content: bytes
    content_type: str
    fetched_at: datetime


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class Fetcher:
    """Fetch a URI after allowlist + per-connector rate-limit checks (G-06)."""

    def __init__(
        self,
        allowlist: Allowlist,
        client: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        min_interval_seconds: float = 1.0,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._allowlist = allowlist
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers=_DEFAULT_HEADERS,
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep_fn = sleep_fn or asyncio.sleep
        self._min_interval_seconds = min_interval_seconds
        self._timeout_seconds = timeout_seconds
        self._last_fetch_at: dict[str, float] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> Fetcher:
        """Build a production Fetcher from Settings (real transport; not for tests).

        The caller owns the underlying ``httpx.AsyncClient`` lifecycle.
        """
        return cls(
            allowlist=Allowlist.from_settings(settings),
            client=httpx.AsyncClient(
                timeout=settings.fetch_timeout_seconds,
                headers=_DEFAULT_HEADERS,
            ),
            min_interval_seconds=settings.fetch_min_interval_seconds,
            timeout_seconds=settings.fetch_timeout_seconds,
        )

    async def fetch(self, uri: str, connector: str = "default") -> FetchedContent:
        """Fetch ``uri`` after the allowlist gate and connector rate-limit check.

        Supports ``file://`` URIs for local mock/demo data (reads from disk).
        """
        # G-06: default-deny — refuse BEFORE any network I/O.
        # Allow file:// URIs through for mock mode (no domain check needed).
        if not uri.startswith("file://"):
            self._allowlist.check(uri)

        now = self._clock()
        last = self._last_fetch_at.get(connector)
        if last is not None:
            elapsed = now.timestamp() - last
            if elapsed < self._min_interval_seconds:
                await self._sleep_fn(self._min_interval_seconds - elapsed)

        # Local file:// fetch (mock / demo mode)
        if uri.startswith("file://"):
            from pathlib import Path

            file_path = Path(uri.removeprefix("file://"))
            _exists = await asyncio.to_thread(file_path.exists)
            if not _exists:
                raise FetchError(f"local file {uri!r} not found")
            content = await asyncio.to_thread(file_path.read_bytes)
            suffix = file_path.suffix.lower()
            content_type = {
                ".html": "text/html",
                ".txt": "text/plain",
                ".rss": "application/rss+xml",
                ".xml": "application/xml",
                ".pdf": "application/pdf",
                ".jsonl": "application/jsonl",
            }.get(suffix, "application/octet-stream")
            self._last_fetch_at[connector] = self._clock().timestamp()
            return FetchedContent(
                uri=uri,
                content=content,
                content_type=content_type,
                fetched_at=self._clock(),
            )

        response = await self._client.get(
            uri,
            follow_redirects=False,
            timeout=self._timeout_seconds,
        )
        self._last_fetch_at[connector] = self._clock().timestamp()

        if response.status_code != 200:
            raise FetchError(f"fetch {uri!r} failed with status {response.status_code}")

        return FetchedContent(
            uri=uri,
            content=response.content,
            content_type=response.headers.get("content-type", ""),
            fetched_at=self._clock(),
        )
