"""Per-stage cost circuit breaker (task_011, G-03).

A run has one ``cost_budget_usd``; each stage is entitled to a fraction of it
(:data:`DEFAULT_STAGE_FRACTION`). The flow checks the breaker after every
non-final stage — total-run budget first, then the stage budget. A breach
raises :class:`CircuitBreakerError`, the flow marks the run ``paused``, emits
the ``circuit_breaker_open`` alert log, and returns ``"paused"`` so the caller
can raise the budget and ``resume_pipeline`` picks up from the last
checkpoint.

Pure and deterministic: no I/O, no Prefect, no DB — trivially unit-testable.
"""

from __future__ import annotations

from decimal import Decimal

# Default stage entitlement: 10% of the run budget per stage.
DEFAULT_STAGE_FRACTION = Decimal("0.10")


class CircuitBreakerError(RuntimeError):
    """Raised when a stage (or the run total) exceeds its cost budget."""


class CircuitBreaker:
    """Compares metered spend against run + per-stage budgets."""

    def __init__(self, stage_fraction: Decimal = DEFAULT_STAGE_FRACTION) -> None:
        self._fraction = stage_fraction

    def stage_budget(self, run_budget: Decimal) -> Decimal:
        """Return the per-stage budget for a run budget (fraction of total)."""
        return run_budget * self._fraction

    def check(self, stage: str, spent: Decimal, run_budget: Decimal) -> None:
        """Raise :class:`CircuitBreakerError` when ``spent`` breaches a budget.

        The total-run budget is checked FIRST (a run may never spend beyond
        its overall cap, even when the stage fraction would allow more).
        """
        if spent > run_budget:
            raise CircuitBreakerError(
                f"total budget breached for {stage}: spent {spent} > run budget {run_budget}"
            )
        budget = self.stage_budget(run_budget)
        if spent > budget:
            raise CircuitBreakerError(
                f"stage budget breached for {stage}: spent {spent} > stage budget "
                f"{budget} (run budget {run_budget} * fraction {self._fraction})"
            )
