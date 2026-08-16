"""Unit tests for ``app.core.metrics`` (task_013 — Prometheus instruments).

Hermetic: pure in-memory ``prometheus_client`` registry — no network, no
Prefect server, no Docker, no LLM. Instrument values are read back through
the registry's sample lookup exactly as the ``/metrics`` endpoint would
export them, and the exposition text round-trips through the parser.
"""

from __future__ import annotations

import re
from decimal import Decimal
from uuid import uuid4

import pytest
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

from app.core import metrics


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """Isolate every test with a fresh in-memory registry."""
    metrics.reset_registry()


def _family_names() -> set[str]:
    """Names of every instrument exported by the module registry.

    Parsed from the raw exposition text (not the parser, which strips the
    ``_total`` suffix from counter families).
    """
    text = generate_latest(metrics.get_registry()).decode()
    return set(re.findall(r"^# HELP (\S+)", text, flags=re.MULTILINE))


def _sample(name: str, labels: dict[str, str] | None = None) -> float | None:
    return metrics.get_registry().get_sample_value(name, labels)


# --------------------------------------------------------------------------- #
# Instrument inventory (brief test 1)
# --------------------------------------------------------------------------- #
def test_all_required_instruments_exist_with_ecrke_prefix() -> None:
    expected = {
        "ecrke_run_cost_spent_usd",
        "ecrke_stage_duration_seconds",
        "ecrke_verification_pass_total",
        "ecrke_verification_fail_total",
        "ecrke_verification_pass_rate",
        "ecrke_contradictions_confirmed_total",
        "ecrke_kb_sources_total",
        "ecrke_kb_passages_total",
        "ecrke_kb_statements_total",
        "ecrke_runs_total",
    }
    assert expected <= _family_names()


def test_record_run_cost_updates_gauge_per_run_id() -> None:
    metrics.record_run_cost("run-abc", 1.25)
    metrics.record_run_cost("run-abc", 2.5)  # last write wins
    metrics.record_run_cost("run-def", 0.75)
    assert _sample("ecrke_run_cost_spent_usd", {"run_id": "run-abc"}) == 2.5
    assert _sample("ecrke_run_cost_spent_usd", {"run_id": "run-def"}) == 0.75


def test_record_run_cost_accepts_uuid_and_decimal() -> None:
    run_id = uuid4()
    metrics.record_run_cost(run_id, Decimal("0.5000"))
    assert _sample("ecrke_run_cost_spent_usd", {"run_id": str(run_id)}) == 0.5


def test_record_stage_duration_records_histogram_buckets() -> None:
    metrics.record_stage_duration("define", 0.25)
    metrics.record_stage_duration("define", 1.5)
    metrics.record_stage_duration("search", 0.05)
    assert _sample("ecrke_stage_duration_seconds_count", {"stage": "define"}) == 2
    assert _sample("ecrke_stage_duration_seconds_count", {"stage": "search"}) == 1
    assert _sample("ecrke_stage_duration_seconds_sum", {"stage": "define"}) == pytest.approx(1.75)


def test_record_verification_maintains_pass_fail_counts_and_rate() -> None:
    for _ in range(3):
        metrics.record_verification(passed=True)
    metrics.record_verification(passed=False)
    assert _sample("ecrke_verification_pass_total") == 3
    assert _sample("ecrke_verification_fail_total") == 1
    # 3 passes + 1 fail -> 0.75 (brief test 2)
    assert _sample("ecrke_verification_pass_rate") == pytest.approx(0.75)


def test_verification_pass_rate_is_zero_before_any_verification() -> None:
    assert _sample("ecrke_verification_pass_rate") == 0.0


def test_record_contradiction_increments_counter() -> None:
    metrics.record_contradiction()
    metrics.record_contradiction()
    assert _sample("ecrke_contradictions_confirmed_total") == 2


def test_record_kb_growth_sets_gauges() -> None:
    metrics.record_kb_growth(sources=5, passages=12, statements=30)
    metrics.record_kb_growth(sources=7, passages=15, statements=41)  # gauge: last wins
    assert _sample("ecrke_kb_sources_total") == 7
    assert _sample("ecrke_kb_passages_total") == 15
    assert _sample("ecrke_kb_statements_total") == 41


def test_record_run_status_increments_counter_by_status_label() -> None:
    metrics.record_run_status("completed")
    metrics.record_run_status("completed")
    metrics.record_run_status("failed")
    assert _sample("ecrke_runs_total", {"status": "completed"}) == 2
    assert _sample("ecrke_runs_total", {"status": "failed"}) == 1


def test_registry_exports_valid_prometheus_text() -> None:
    text = generate_latest(metrics.get_registry()).decode()
    assert text.startswith("# HELP")
    for name in (
        "ecrke_run_cost_spent_usd",
        "ecrke_stage_duration_seconds",
        "ecrke_verification_pass_rate",
        "ecrke_runs_total",
    ):
        assert name in text
    # the exposition parses cleanly (valid text format)
    list(text_string_to_metric_families(text))
