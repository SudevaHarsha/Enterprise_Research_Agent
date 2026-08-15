"""Repeat-call cache backed by the ``kv_cache`` table (replaces Redis).

Cache key = hash(model + prompt + inputs); expiry is honored and expired
entries are treated as misses. Payloads carry only answer data — never
credentials, never request messages (Ironclad Rule 01, G-05). The session
factory and clock are injectable so unit tests stay hermetic (no database,
no network).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.db.models import KVEntry
from app.db.session import async_session_factory

logger = get_logger("app.services.kv_cache")

_SECRET_KEY_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "api_key",
    "apikey",
    "_key",
)


def _is_secret_key(key: str) -> bool:
    """Return True when a payload key hints that its value is a credential."""
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)


def _canonical_json(value: Any) -> str:
    """JSON-safe, order-insensitive canonicalization for hash inputs."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def build_key(
    model: str,
    prompt: str,
    inputs: Mapping[str, Any] | None = None,
) -> str:
    """Return the 64-char cache key: sha256(model + prompt + inputs)."""
    canonical = f"{model}|{prompt}|{_canonical_json(dict(inputs or {}))}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_prompt_hash(prompt: str, inputs: Mapping[str, Any] | None = None) -> str:
    """Return the 64-char prompt hash column value: sha256(prompt + inputs)."""
    canonical = f"{prompt}|{_canonical_json(dict(inputs or {}))}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _assert_no_secret_payload(payload: Mapping[str, Any]) -> None:
    """Reject payloads whose keys hint at credentials (Ironclad Rule 01, G-05)."""
    for key in payload:
        if _is_secret_key(str(key)):
            raise ValueError(
                f"Cache payload key {key!r} looks like a secret/credential; "
                "credentials must never be persisted in the kv_cache (Rule 01, G-05)."
            )


class KVCache:
    """Cache-aside store over the ``kv_cache`` table with injectable factory/clock."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the payload for ``key`` if present and unexpired, else None."""
        async with self._session_factory() as session:
            entry = await session.get(KVEntry, key)
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= self._clock():
                await session.delete(entry)
                await session.commit()
                return None
        return dict(entry.payload)

    async def set(
        self,
        key: str,
        *,
        model: str,
        prompt_hash: str,
        payload: dict[str, Any],
        ttl_seconds: int | None = None,
    ) -> None:
        """Upsert ``key`` with a payload; ``ttl_seconds=None`` never expires."""
        _assert_no_secret_payload(payload)
        expires_at: datetime | None = None
        if ttl_seconds is not None:
            expires_at = self._clock() + timedelta(seconds=ttl_seconds)
        async with self._session_factory() as session:
            entry = await session.get(KVEntry, key)
            if entry is None:
                session.add(
                    KVEntry(
                        key=key,
                        model=model,
                        prompt_hash=prompt_hash,
                        payload=payload,
                        expires_at=expires_at,
                    )
                )
            else:
                entry.model = model
                entry.prompt_hash = prompt_hash
                entry.payload = payload
                entry.expires_at = expires_at
            await session.commit()

    async def delete(self, key: str) -> None:
        """Remove an entry if present (used for expired-entry cleanup)."""
        async with self._session_factory() as session:
            entry = await session.get(KVEntry, key)
            if entry is not None:
                await session.delete(entry)
                await session.commit()
