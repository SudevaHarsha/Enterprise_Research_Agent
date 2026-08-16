"""CircuitBreaker unit tests (task_011 — per-stage cost budgets + pause).

Hermetic and pure: no Prefect, no DB. Verifies the documented default stage
fraction constant, the pass case, the per-stage breach, the total-run breach
(checked first), and the budget math helpers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.pipeline.circuit_breaker import (
    DEFAULT_STAGE_FRACTION,
    CircuitBreaker,
    CircuitBreakerError,
)


async def test_default_stage_fraction_is_documented_constant() -> None:
    assert Decimal("0.10") == DEFAULT_STAGE_FRACTION


async def test_stage_budget_is_fraction_of_run_budget() -> None:
    breaker = CircuitBreaker()
    assert breaker.stage_budget(Decimal("5.00")) == Decimal("0.50")
    assert breaker.stage_budget(Decimal("10.00")) == Decimal("1.00")


async def test_check_passes_under_stage_budget() -> None:
    breaker = CircuitBreaker(stage_fraction=Decimal("0.10"))
    # spent 0.40 <= stage budget 0.50 (5.00 * 0.10) and <= total 5.00
    breaker.check("collect", Decimal("0.40"), Decimal("5.00"))


async def test_check_passes_when_spent_equals_stage_budget() -> None:
    breaker = CircuitBreaker(stage_fraction=Decimal("0.10"))
    breaker.check("collect", Decimal("0.50"), Decimal("5.00"))


async def test_check_raises_over_stage_budget() -> None:
    breaker = CircuitBreaker(stage_fraction=Decimal("0.10"))
    with pytest.raises(CircuitBreakerError, match="collect"):
        breaker.check("collect", Decimal("0.60"), Decimal("5.00"))


async def test_check_raises_over_total_run_budget_first() -> None:
    # fraction 1.0 => stage budget == total; the total branch must fire first.
    breaker = CircuitBreaker(stage_fraction=Decimal("1.00"))
    with pytest.raises(CircuitBreakerError, match="total"):
        breaker.check("trace", Decimal("5.01"), Decimal("5.00"))


async def test_error_message_includes_budgets() -> None:
    breaker = CircuitBreaker(stage_fraction=Decimal("0.10"))
    with pytest.raises(CircuitBreakerError, match="0.50") as excinfo:
        breaker.check("collect", Decimal("0.60"), Decimal("5.00"))
    message = str(excinfo.value)
    assert "0.60" in message
    assert "5.00" in message


async def test_zero_budget_breaches_on_any_spend() -> None:
    breaker = CircuitBreaker()
    with pytest.raises(CircuitBreakerError):
        breaker.check("define", Decimal("0.0001"), Decimal("0.00"))
