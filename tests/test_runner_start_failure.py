"""Hermetic tests for the pipeline-runner start-failure persistence (DoD fix).

A run must never be stuck in ``submitted`` when the pipeline cannot start
(e.g. ``SEARCH_PROVIDER`` unset, LLM keys missing, worker submission error).
``DefaultPipelineRunner`` persists ``failed`` + an immutable audit row for any
run that could not leave the starting gate — the durable run row is the
observable contract an evaluator polls (build-plan Step 15 DoD finding).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.api.deps import DefaultPipelineRunner
from app.core.config import Settings
from app.db.models import AuditTrace, Run
from tests.conftest import FakeSessionFactory, rows_of


def _build_settings(prefect_api_url: str = "") -> Settings:
    return Settings(
        app_env="test",
        database_url="postgresql+asyncpg://ecrke:ecrke_dev@localhost:5433/ecrke",
        prefect_api_url=prefect_api_url,
        llm_model_cheap="gemini/gemini-2.0-flash",
        llm_model_strong="gemini/gemini-2.0-pro",
        run_budget_usd=Decimal("2.0"),
    )


def _seed_submitted_run(factory: FakeSessionFactory) -> Run:
    run = Run(
        id=uuid4(),
        tenant_id=uuid4(),
        question="How does generative AI change software testing practices?",
        status="submitted",
        stage=None,
        progress=0.0,
        cost_budget_usd=Decimal("2.0000"),
        cost_spent_usd=Decimal("0.0000"),
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        completed_at=None,
    )
    factory.storage[run.id] = run
    return run


async def test_runner_persists_failed_when_services_unavailable() -> None:
    """Build failure (e.g. unset SEARCH_PROVIDER) transitions submitted -> failed
    and writes an immutable audit row, so the run is never stuck."""
    factory = FakeSessionFactory()
    run = _seed_submitted_run(factory)

    def explode(_s: Settings, _f: FakeSessionFactory) -> Any:
        raise RuntimeError("SEARCH_PROVIDER is not configured")

    runner = DefaultPipelineRunner(
        session_factory=factory,
        settings=_build_settings(),
        build_services=explode,
    )

    outcome = await runner.run(run.id, None)
    assert outcome == "failed"
    persisted = rows_of(factory.storage, Run)[0]
    assert persisted.id == run.id
    assert persisted.status == "failed"
    audit = rows_of(factory.storage, AuditTrace)
    assert len(audit) == 1
    assert audit[0].action == "run.failed"
    assert audit[0].decision == "failed"
    assert "SEARCH_PROVIDER" in (audit[0].reason or "")


async def test_runner_persists_failed_when_worker_submit_fails() -> None:
    """Worker-mode submission failure also transitions submitted -> failed with
    an audit row (never a stuck submitted run)."""
    factory = FakeSessionFactory()
    run = _seed_submitted_run(factory)

    def broken_client(_url: str) -> Any:
        raise ConnectionError("prefect server unreachable")

    runner = DefaultPipelineRunner(
        session_factory=factory,
        settings=_build_settings(prefect_api_url="http://localhost:4200/api"),
        client_factory=broken_client,
    )

    outcome = await runner.run(run.id, None)
    assert outcome == "failed"
    persisted = rows_of(factory.storage, Run)[0]
    assert persisted.status == "failed"
    audit = rows_of(factory.storage, AuditTrace)
    assert len(audit) == 1
    assert audit[0].action == "run.failed"


async def test_runner_does_not_overwrite_flow_terminal_state() -> None:
    """A run that the flow already moved to a terminal state is left untouched
    (no duplicate audit rows / no status clobbering)."""
    factory = FakeSessionFactory()
    run = _seed_submitted_run(factory)
    run.status = "paused"

    def explode(_s: Settings, _f: FakeSessionFactory) -> Any:
        raise RuntimeError("boom")

    runner = DefaultPipelineRunner(
        session_factory=factory,
        settings=_build_settings(),
        build_services=explode,
    )
    outcome = await runner.run(run.id, None)
    assert outcome == "failed"
    persisted = rows_of(factory.storage, Run)[0]
    assert persisted.status == "paused"  # untouched
    assert rows_of(factory.storage, AuditTrace) == []


async def test_runner_missing_run_is_noop() -> None:
    """A run id with no row is safe (nothing to transition)."""
    factory = FakeSessionFactory()

    def explode(_s: Settings, _f: FakeSessionFactory) -> Any:
        raise RuntimeError("boom")

    runner = DefaultPipelineRunner(
        session_factory=factory,
        settings=_build_settings(),
        build_services=explode,
    )
    assert await runner.run(uuid4(), None) == "failed"
    assert rows_of(factory.storage, AuditTrace) == []
