"""Unit tests for the ``kv_cache`` service (hermetic — fake session, no DB).

Covers: key = hash(model+prompt+inputs), set/get round-trip, expiry honored
(expired entries are misses and get refreshed), upsert semantics, and the
Ironclad Rule 01 guard that cache payloads never carry credentials.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.kv_cache import KVCache, build_key, build_prompt_hash
from tests.conftest import FakeClock, FakeSessionFactory


def test_build_key_is_64_char_sha256() -> None:
    """The cache key is a 64-char sha256 hex digest."""
    key = build_key("model", "prompt")
    assert len(key) == 64
    assert set(key) <= set("0123456789abcdef")


def test_build_key_incorporates_model_prompt_and_inputs() -> None:
    """hash(model+prompt+inputs) changes when any component changes."""
    base = build_key("model", "prompt", {"passage": "a"})
    assert base == build_key("model", "prompt", {"passage": "a"})
    assert base != build_key("model2", "prompt", {"passage": "a"})
    assert base != build_key("model", "prompt2", {"passage": "a"})
    assert base != build_key("model", "prompt", {"passage": "b"})
    assert base != build_key("model", "prompt")  # inputs participate


def test_build_key_ignores_input_ordering() -> None:
    """Canonical JSON means dict ordering does not change the key."""
    assert build_key("m", "p", {"a": 1, "b": 2}) == build_key("m", "p", {"b": 2, "a": 1})


def test_build_prompt_hash_is_stable() -> None:
    """Prompt hash is stable and input-sensitive."""
    assert build_prompt_hash("prompt", {"x": 1}) == build_prompt_hash("prompt", {"x": 1})
    assert build_prompt_hash("prompt", {"x": 1}) != build_prompt_hash("prompt", {"x": 2})


async def test_set_then_get_roundtrip(fake_session_factory: FakeSessionFactory) -> None:
    """A stored payload is returned verbatim."""
    cache = KVCache(session_factory=fake_session_factory)
    key = build_key("model", "prompt")
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "cached answer"},
    )
    assert await cache.get(key) == {"content": "cached answer"}


async def test_get_missing_key_returns_none(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """A key that was never set is a miss."""
    cache = KVCache(session_factory=fake_session_factory)
    assert await cache.get(build_key("model", "missing")) is None


async def test_expired_entry_is_treated_as_miss_and_refreshed(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Expired entries are misses; a fresh set refreshes the value."""
    clock = FakeClock()
    cache = KVCache(session_factory=fake_session_factory, clock=clock)
    key = build_key("model", "prompt")
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "v1"},
        ttl_seconds=60,
    )
    assert await cache.get(key) == {"content": "v1"}

    clock.advance(61)
    assert await cache.get(key) is None  # expired -> miss

    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "v2"},
        ttl_seconds=60,
    )
    assert await cache.get(key) == {"content": "v2"}  # refreshed


async def test_entry_without_ttl_never_expires(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """ttl_seconds=None means the entry never expires."""
    clock = FakeClock()
    cache = KVCache(session_factory=fake_session_factory, clock=clock)
    key = build_key("model", "prompt")
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "v1"},
        ttl_seconds=None,
    )
    clock.advance(10_000)
    assert await cache.get(key) == {"content": "v1"}


async def test_upsert_replaces_existing_payload(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Setting the same key twice keeps one row with the newest payload."""
    cache = KVCache(session_factory=fake_session_factory)
    key = build_key("model", "prompt")
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "first"},
    )
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "second"},
        ttl_seconds=30,
    )
    assert await cache.get(key) == {"content": "second"}
    assert len(fake_session_factory.storage) == 1


async def test_set_rejects_payload_containing_credentials(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Cache payloads must never contain credentials (Ironclad Rule 01, G-05)."""
    cache = KVCache(session_factory=fake_session_factory)
    key = build_key("model", "prompt")
    with pytest.raises(ValueError, match="secret"):
        await cache.set(
            key,
            model="model",
            prompt_hash=build_prompt_hash("prompt"),
            payload={"content": "answer", "api_key": "sk-credential-value"},
        )
    assert await cache.get(key) is None


async def test_expired_entry_removed_from_storage(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Expired entries are purged, not just hidden."""
    clock = FakeClock()
    cache = KVCache(session_factory=fake_session_factory, clock=clock)
    key = build_key("model", "prompt")
    await cache.set(
        key,
        model="model",
        prompt_hash=build_prompt_hash("prompt"),
        payload={"content": "v1"},
        ttl_seconds=1,
    )
    clock.advance(timedelta(seconds=2).total_seconds())
    assert await cache.get(key) is None
    assert key not in fake_session_factory.storage
