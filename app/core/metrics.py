"""Prometheus metrics for ECRKE (task_013, design doc §14).

All instruments live on a module-level :class:`~prometheus_client.CollectorRegistry`
rendered by ``GET /metrics`` (``app.api.metrics``) via ``generate_latest``.
Tracing stays OpenTelemetry (``app.core.telemetry``); this module is metrics-only
and intentionally does not pull in any OpenTelemetry exporter.

Privacy choice (G-05 / Rule 01): ``ecrke_run_cost_spent_usd`` is a Gauge keyed by
``run_id`` — run ids are opaque UUIDs, not PII, and the gauge is the per-run cost
ledger the design doc asks for. Everything else prefers aggregate counters/gauges
with no run-scoped labels so no identifying values leak into exported series.

``reset_registry`` exists for test isolation (hermetic in-memory registries); the
``/metrics`` endpoint always renders the live module registry.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

_registry: CollectorRegistry
run_cost_spent_usd: Gauge
stage_duration_seconds: Histogram
verification_pass_total: Counter
verification_fail_total: Counter
verification_pass_rate: Gauge
contradictions_confirmed_total: Counter
kb_sources_total: Gauge
kb_passages_total: Gauge
kb_statements_total: Gauge
runs_total: Counter


def _build_instruments() -> None:
    """(Re)create every instrument bound to a fresh module registry."""
    global _registry
    global run_cost_spent_usd
    global stage_duration_seconds
    global verification_pass_total
    global verification_fail_total
    global verification_pass_rate
    global contradictions_confirmed_total
    global kb_sources_total
    global kb_passages_total
    global kb_statements_total
    global runs_total

    _registry = CollectorRegistry()
    run_cost_spent_usd = Gauge(
        "ecrke_run_cost_spent_usd",
        "USD metered spend of a research run (G-03 cost observability). "
        "Keyed by run_id: opaque UUID, not PII.",
        labelnames=("run_id",),
        registry=_registry,
    )
    stage_duration_seconds = Histogram(
        "ecrke_stage_duration_seconds",
        "Duration of one pipeline stage execution, by stage.",
        labelnames=("stage",),
        registry=_registry,
    )
    verification_pass_total = Counter(
        "ecrke_verification_pass_total",
        "Total statements passing verification (verify-first gate).",
        registry=_registry,
    )
    verification_fail_total = Counter(
        "ecrke_verification_fail_total",
        "Total statements failing verification (verify-first gate).",
        registry=_registry,
    )
    verification_pass_rate = Gauge(
        "ecrke_verification_pass_rate",
        "Verified statements / total verified statements, 0..1.",
        registry=_registry,
    )
    contradictions_confirmed_total = Counter(
        "ecrke_contradictions_confirmed_total",
        "Total confirmed (not merely flagged) contradictions.",
        registry=_registry,
    )
    kb_sources_total = Gauge(
        "ecrke_kb_sources_total",
        "Knowledge-base sources currently stored.",
        registry=_registry,
    )
    kb_passages_total = Gauge(
        "ecrke_kb_passages_total",
        "Knowledge-base passages currently stored.",
        registry=_registry,
    )
    kb_statements_total = Gauge(
        "ecrke_kb_statements_total",
        "Knowledge-base statements currently stored.",
        registry=_registry,
    )
    runs_total = Counter(
        "ecrke_runs_total",
        "Total research runs by terminal status.",
        labelnames=("status",),
        registry=_registry,
    )


_build_instruments()


def get_registry() -> CollectorRegistry:
    """Return the live module registry (rendered by ``GET /metrics``)."""
    return _registry


def reset_registry() -> CollectorRegistry:
    """Replace the module registry with fresh instruments (test isolation)."""
    _build_instruments()
    return _registry


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Read one sample value from the live registry (0.0 when never set)."""
    value = _registry.get_sample_value(name, labels)
    return value if value is not None else 0.0


def record_run_cost(run_id: UUID | str, usd: Decimal | float | int) -> None:
    """Set the per-run cost gauge (last write wins per run)."""
    run_cost_spent_usd.labels(run_id=str(run_id)).set(float(usd))


def record_stage_duration(stage: str, seconds: float) -> None:
    """Record one successful stage execution duration (histogram)."""
    stage_duration_seconds.labels(stage=stage).observe(max(0.0, float(seconds)))


def record_verification(passed: bool) -> None:
    """Record one verification verdict and refresh the pass-rate gauge."""
    if passed:
        verification_pass_total.inc()
    else:
        verification_fail_total.inc()
    total = _sample("ecrke_verification_pass_total") + _sample("ecrke_verification_fail_total")
    passes = _sample("ecrke_verification_pass_total")
    rate = passes / total if total > 0 else 0.0
    verification_pass_rate.set(rate)


def record_contradiction() -> None:
    """Record one confirmed contradiction."""
    contradictions_confirmed_total.inc()


def record_kb_growth(sources: int, passages: int, statements: int) -> None:
    """Set the knowledge-base size gauges (current totals, last write wins)."""
    kb_sources_total.set(int(sources))
    kb_passages_total.set(int(passages))
    kb_statements_total.set(int(statements))


def record_run_status(status: str) -> None:
    """Record one run reaching a terminal status (completed/paused/failed)."""
    runs_total.labels(status=status).inc()


__all__: list[str] = [
    "get_registry",
    "reset_registry",
    "record_run_cost",
    "record_stage_duration",
    "record_verification",
    "record_contradiction",
    "record_kb_growth",
    "record_run_status",
]
