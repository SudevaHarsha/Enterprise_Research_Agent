"""Unit tests for the cost meter (hermetic — fake session, no DB).

Covers: incrementing ``runs.cost_spent_usd`` by the reported cost (Numeric,
4 decimal places), tiktoken estimate fallback when provider cost is unknown,
never-negative accounting (G-03), and safe standalone metering when no
``run_id`` is supplied.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.cost_meter import (
    CostMeter,
    NegativeCostError,
    RunNotFoundError,
    estimate_cost,
)
from tests.conftest import FakeResponse, FakeSessionFactory, make_run_row


def test_estimate_cost_fallback_is_positive_and_deterministic() -> None:
    """tiktoken fallback returns a positive, deterministic Decimal."""
    first = estimate_cost(model="fake/cheap", prompt="hello world", completion="hi")
    second = estimate_cost(model="fake/cheap", prompt="hello world", completion="hi")
    assert first == second
    assert first > 0
    assert isinstance(first, Decimal)


def test_estimate_cost_uses_supplied_token_counts() -> None:
    """Explicit token counts win over text counting."""
    cost = estimate_cost(model="fake/cheap", prompt_tokens=100, completion_tokens=50)
    assert cost == Decimal(150) * Decimal("0.000002")


async def test_record_call_increments_run_cost_spent_usd(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """A successful call increments runs.cost_spent_usd by the reported cost."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )

    cost = await meter.record_call(
        model="fake/cheap",
        run_id=run.id,
        response=FakeResponse("answer"),
    )

    assert cost == Decimal("0.0010")
    assert run.cost_spent_usd == Decimal("0.0010")


async def test_record_call_accumulates_across_calls(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """Repeated calls accumulate on the run, rounded to 4 decimals."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )

    await meter.record_call(model="fake/cheap", run_id=run.id, response=FakeResponse("a"))
    await meter.record_call(model="fake/cheap", run_id=run.id, response=FakeResponse("b"))

    assert run.cost_spent_usd == Decimal("0.0020")


async def test_record_call_accepts_str_run_id(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """run_id may be passed as a string (as pipeline stages will)."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0005"),
    )

    await meter.record_call(model="fake/strong", run_id=str(run.id), response=FakeResponse("a"))

    assert run.cost_spent_usd == Decimal("0.0005")


async def test_record_call_estimate_fallback_when_provider_cost_unknown(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """When the provider cost is unknown, the tiktoken estimate is recorded."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: None,  # provider cost unknown
    )

    cost = await meter.record_call(
        model="fake/cheap",
        run_id=run.id,
        response=FakeResponse("answer", prompt_tokens=100, completion_tokens=50),
    )

    assert cost == Decimal(150) * Decimal("0.000002")
    assert run.cost_spent_usd == cost
    assert run.cost_spent_usd > 0


async def test_record_call_never_negative(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """A negative cost raises (G-03: never negative) and is never persisted."""
    run = make_run_row(cost_spent_usd=Decimal("0.0000"))
    fake_session_factory.storage[run.id] = run
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("-0.0010"),
    )

    with pytest.raises(NegativeCostError):
        await meter.record_call(
            model="fake/cheap",
            run_id=run.id,
            response=FakeResponse("answer"),
        )
    assert run.cost_spent_usd == Decimal("0.0000")


async def test_record_call_standalone_without_run_is_safe(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """No run_id -> returns the metered cost without persisting anything."""
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0025"),
    )

    cost = await meter.record_call(
        model="fake/cheap",
        run_id=None,
        response=FakeResponse("answer"),
    )

    assert cost == Decimal("0.0025")
    assert fake_session_factory.storage == {}


async def test_record_call_missing_run_raises(
    fake_session_factory: FakeSessionFactory,
) -> None:
    """A run_id that does not resolve fails loud instead of silently dropping cost."""
    meter = CostMeter(
        session_factory=fake_session_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )

    with pytest.raises(RunNotFoundError):
        await meter.record_call(
            model="fake/cheap",
            run_id="00000000-0000-0000-0000-000000000000",
            response=FakeResponse("answer"),
        )
