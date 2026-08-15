"""Provider-agnostic search connector (task_005).

The connector is intentionally thin: a configured provider name plus an
injected async search callable. Tests inject a fake search backend; production
providers (brave, serpapi) plug in behind the same signature. Missing or
unknown ``SEARCH_PROVIDER`` values fail fast with a clear configuration error
so misconfiguration is never silently retried.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core.config import Settings

SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"mock", "brave", "serpapi"})


class SearchProviderError(ValueError):
    """Raised when SEARCH_PROVIDER is missing or names an unsupported provider."""


class SearchConnector:
    """Return candidate URIs for a query via an injected provider backend."""

    def __init__(
        self,
        provider: str,
        *,
        search_fn: Callable[[str, int], Awaitable[list[str]]] | None = None,
        default_limit: int = 10,
    ) -> None:
        self.provider = (provider or "").strip().lower()
        if not self.provider:
            raise SearchProviderError(
                "SEARCH_PROVIDER is not configured; set SEARCH_PROVIDER "
                "(supported: " + ", ".join(sorted(SUPPORTED_PROVIDERS)) + ")"
            )
        if self.provider not in SUPPORTED_PROVIDERS:
            raise SearchProviderError(
                f"unsupported SEARCH_PROVIDER {self.provider!r} "
                f"(supported: {', '.join(sorted(SUPPORTED_PROVIDERS))})"
            )
        self._search_fn = search_fn
        self._default_limit = default_limit

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        search_fn: Callable[[str, int], Awaitable[list[str]]] | None = None,
    ) -> SearchConnector:
        """Build a connector from Settings (SEARCH_PROVIDER wins over legacy field)."""
        provider = (settings.search_provider or settings.search_api_provider or "").strip()
        return cls(
            provider=provider,
            search_fn=search_fn,
            default_limit=settings.search_results_limit,
        )

    async def search(self, query: str, limit: int | None = None) -> list[str]:
        """Return candidate URIs for ``query`` (empty when no backend is wired)."""
        if self._search_fn is None:
            raise SearchProviderError(
                f"SEARCH_PROVIDER {self.provider!r} has no backend wired for this environment"
            )
        effective_limit = limit or self._default_limit
        return await self._search_fn(query, effective_limit)
