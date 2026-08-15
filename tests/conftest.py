"""Shared pytest fixtures for the ECRKE test suite.

Hermetic service-test helpers: LiteLLM-shaped fake responses/providers and an
in-memory async-session stand-in so the gateway / cost-meter / kv-cache unit
tests never touch a real database, network, or LLM provider (no real LLM API
calls in tests — task_004 constraint).
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field

from app.db.models import KVEntry, Run


class SampleOutput(BaseModel):
    """Small Pydantic model used to exercise structured-output gateway paths."""

    topic: str
    confidence: float = Field(ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class FakeUsage:
    """LiteLLM-shaped usage metadata."""

    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 5) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class FakeMessage:
    """LiteLLM-shaped assistant message."""

    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoice:
    """LiteLLM-shaped choice wrapper."""

    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeResponse:
    """LiteLLM-shaped completion response (object attribute access)."""

    def __init__(
        self,
        content: str,
        prompt_tokens: int = 10,
        completion_tokens: int = 5,
    ) -> None:
        self.choices = [FakeChoice(content)]
        self.usage = FakeUsage(prompt_tokens, completion_tokens)


class FakeProvider:
    """Async callable mimicking ``litellm.acompletion``; records every call."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self._error: Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse("hello from fake provider")

    def queue(self, response: Any) -> None:
        """Queue a response to be returned on the next provider call."""
        self._responses.append(response)

    def set_error(self, error: Exception) -> None:
        """Make the provider raise ``error`` on every subsequent call."""
        self._error = error


class FakeClock:
    """Controllable clock for expiry tests."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or datetime.now(UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Advance the clock by ``seconds``."""
        self._now += timedelta(seconds=seconds)


class FakeSession:
    """In-memory stand-in for ``AsyncSession`` covering the service surface used."""

    def __init__(self, storage: dict[Any, Any]) -> None:
        self._storage = storage
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None

    async def get(self, model: type[Any], key: Any) -> Any | None:
        if isinstance(key, str):
            with contextlib.suppress(ValueError):
                key = UUID(key)
        return self._storage.get(key)

    def add(self, obj: Any) -> None:
        if isinstance(obj, KVEntry):
            self._storage[obj.key] = obj
        elif isinstance(obj, Run):
            self._storage[obj.id] = obj
        else:
            raise TypeError(f"FakeSession.add does not support {type(obj).__name__}")

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, KVEntry):
            self._storage.pop(obj.key, None)
        elif isinstance(obj, Run):
            self._storage.pop(obj.id, None)
        else:
            raise TypeError(f"FakeSession.delete does not support {type(obj).__name__}")


class FakeSessionFactory:
    """Callable that hands out ``FakeSession`` objects over one shared storage dict."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        self.storage: dict[Any, Any] = storage if storage is not None else {}

    def __call__(self) -> FakeSession:
        return FakeSession(self.storage)


@pytest.fixture
def fake_session_factory() -> FakeSessionFactory:
    """Fixture: fresh in-memory session factory per test."""
    return FakeSessionFactory()


@pytest.fixture
def fake_provider() -> FakeProvider:
    """Fixture: provider that records calls and returns canned responses."""
    return FakeProvider()


def make_run_row(cost_spent_usd: Decimal | float = Decimal("0.0000")) -> Run:
    """Build a ``Run`` ORM instance without a database (pre-insert object)."""
    return Run(
        id=uuid4(),
        tenant_id=uuid4(),
        question="test question",
        cost_spent_usd=cost_spent_usd,
    )
