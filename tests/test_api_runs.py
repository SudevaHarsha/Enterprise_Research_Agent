"""API tests: runs lifecycle, runner injection, tenancy, evaluator flow (task_012).

Hermetic: TestClient + FastAPI ``dependency_overrides`` + FakeSession only —
no real LLM, DB, Docker, or network. Covers the brief's run-surface cases:
OpenAPI renders all 10 ``/v1`` paths; POST /v1/runs creates a submitted run
(422 on empty question); execute=true drives the injected runner; GET run
returns lifecycle fields; stages come from checkpoints; conclusions carry
evidence links; resume drives the resume runner; cross-tenant GET/resume are
403; and the evaluator flow (submit -> poll -> conclusions -> trace) works
end-to-end over HTTP.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_runner, get_session_factory
from app.db.models import (
    Checkpoint,
    Conclusion,
    ConclusionEvidence,
    EvidenceLink,
    Passage,
    Run,
    Source,
    Statement,
    Tenant,
)
from app.main import app
from tests.conftest import FakeSessionFactory, rows_of


class RecordingRunner:
    """Fake PipelineRunner recording every run/resume call (hermetic)."""

    def __init__(self) -> None:
        self.run_calls: list[UUID | str] = []
        self.resume_calls: list[UUID | str] = []

    async def run(self, run_id: UUID | str, services: Any | None = None) -> str:
        self.run_calls.append(run_id)
        return "completed"

    async def resume(self, run_id: UUID | str, services: Any | None = None) -> str:
        self.resume_calls.append(run_id)
        return "completed"


class CompletingRunner:
    """Fake runner that simulates a completed pipeline run in storage."""

    def __init__(self, factory: FakeSessionFactory) -> None:
        self._factory = factory

    async def run(self, run_id: UUID | str, services: Any | None = None) -> str:
        storage = self._factory.storage
        run = next(r for r in storage.values() if isinstance(r, Run) and r.id == run_id)
        run.status = "completed"
        run.stage = "done"
        run.progress = 1.0
        run.completed_at = datetime.now(UTC)
        source = Source(
            id=uuid4(),
            run_id=run_id,
            uri="https://retail.example.com/report",
            title="Retail report",
            source_type="web",
            content_hash="h1",
            status="normalized",
        )
        passage = Passage(
            id=uuid4(),
            source_id=source.id,
            seq=0,
            text="Retailers adopt AI for inventory management.",
            hash="ph1",
        )
        statement = Statement(
            id=uuid4(),
            run_id=run_id,
            passage_id=passage.id,
            text="AI adoption is growing in retail operations.",
            status="verified",
            confidence=0.9,
        )
        conclusion = Conclusion(
            id=uuid4(),
            run_id=run_id,
            text="AI is transforming retail operations.",
            confidence=0.9,
            human_review_required=False,
        )
        storage[source.id] = source
        storage[passage.id] = passage
        storage[statement.id] = statement
        storage[conclusion.id] = conclusion
        storage[(conclusion.id, statement.id)] = ConclusionEvidence(
            conclusion_id=conclusion.id, statement_id=statement.id
        )
        storage[statement.id] = statement
        storage[uuid4()] = EvidenceLink(
            id=uuid4(),
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=run_id,
            score="full",
            method="verify",
        )
        return "completed"

    async def resume(self, run_id: UUID | str, services: Any | None = None) -> str:
        return "completed"


def seed_tenant(storage: dict[Any, Any], namespace: str = "default") -> Tenant:
    """Insert a Tenant row by namespace (FakeSession cannot add Tenant)."""
    tenant = Tenant(id=uuid4(), name=namespace.title(), namespace=namespace, rbac_policy={})
    storage[tenant.id] = tenant
    return tenant


def seed_run(
    storage: dict[Any, Any],
    tenant_id: UUID,
    *,
    question: str = "How is AI transforming retail operations?",
    status: str = "completed",
    stage: str | None = "done",
    progress: float = 1.0,
) -> Run:
    """Insert a Run row owned by ``tenant_id``."""
    run = Run(
        id=uuid4(),
        tenant_id=tenant_id,
        question=question,
        status=status,
        stage=stage,
        progress=progress,
        cost_budget_usd=Decimal("2.0000"),
        cost_spent_usd=Decimal("0.1234"),
        created_at=datetime.now(UTC),
    )
    storage[run.id] = run
    return run


@pytest.fixture
def api() -> Iterator[tuple[TestClient, FakeSessionFactory]]:
    """TestClient with fresh FakeSessionFactory overrides (cleared after use)."""
    app.dependency_overrides.clear()
    factory = FakeSessionFactory()
    app.dependency_overrides[get_session_factory] = lambda: factory
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# OpenAPI
# --------------------------------------------------------------------------- #
def test_openapi_renders_all_ten_v1_paths() -> None:
    schema = app.openapi()
    paths = set(schema["paths"])
    expected = {
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/stages",
        "/v1/runs/{run_id}/conclusions",
        "/v1/statements/{statement_id}/trace",
        "/v1/runs/{run_id}/contradictions",
        "/v1/runs/{run_id}/report",
        "/v1/runs/{run_id}/resume",
        "/v1/kb/search",
        "/v1/runs/{run_id}/audit",
    }
    assert expected <= paths


# --------------------------------------------------------------------------- #
# POST /v1/runs
# --------------------------------------------------------------------------- #
def test_create_run_creates_row_and_returns_id(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    response = client.post(
        "/v1/runs", json={"question": "How is AI transforming retail?", "execute": False}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "submitted"
    assert body["question"] == "How is AI transforming retail?"
    runs = rows_of(factory.storage, Run)
    assert len(runs) == 1
    assert str(runs[0].id) == body["run_id"]
    assert str(runs[0].tenant_id) == body["tenant_id"]
    assert body["progress"] == 0.0
    assert body["cost_spent_usd"] == 0.0


def test_create_run_422_on_empty_question(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    assert client.post("/v1/runs", json={"question": ""}).status_code == 422
    assert client.post("/v1/runs", json={"question": "   "}).status_code == 422
    assert client.post("/v1/runs", json={}).status_code == 422
    assert not rows_of(factory.storage, Run)


def test_create_run_execute_true_invokes_injected_runner(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    runner = RecordingRunner()
    app.dependency_overrides[get_runner] = lambda: runner
    response = client.post(
        "/v1/runs",
        json={
            "question": "How is AI transforming retail?",
            "execute": True,
            "cost_budget_usd": 5.0,
        },
    )
    assert response.status_code == 201
    run_id = UUID(response.json()["run_id"])
    assert runner.run_calls == [run_id]
    assert runner.resume_calls == []


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}
# --------------------------------------------------------------------------- #
def test_get_run_returns_lifecycle_fields(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    response = client.get(f"/v1/runs/{run.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run.id)
    assert body["tenant_id"] == str(tenant.id)
    assert body["question"] == run.question
    assert body["status"] == "completed"
    assert body["stage"] == "done"
    assert body["progress"] == 1.0
    assert body["cost_budget_usd"] == 2.0
    assert body["cost_spent_usd"] == 0.1234
    assert body["created_at"] is not None


def test_get_run_404_unknown_id(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    response = client.get(f"/v1/runs/{uuid4()}")
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/stages
# --------------------------------------------------------------------------- #
def test_get_stages_returns_checkpoint_info(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    now = datetime.now(UTC)
    factory.storage[uuid4()] = Checkpoint(
        id=uuid4(),
        run_id=run.id,
        stage="search",
        state={"urls": ["https://retail.example.com/a"]},
        ts=now,
    )
    factory.storage[uuid4()] = Checkpoint(
        id=uuid4(),
        run_id=run.id,
        stage="collect",
        state={"sources": 2},
        ts=now,
    )
    response = client.get(f"/v1/runs/{run.id}/stages")
    assert response.status_code == 200
    stages = response.json()
    assert {stage["stage"] for stage in stages} == {"search", "collect"}
    by_stage = {stage["stage"]: stage for stage in stages}
    assert by_stage["search"]["summary"] == {"urls": ["https://retail.example.com/a"]}
    assert by_stage["search"]["ts"] is not None


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/conclusions
# --------------------------------------------------------------------------- #
def test_get_conclusions_returns_evidence_links(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    conclusion = Conclusion(
        id=uuid4(),
        run_id=run.id,
        text="AI transforms retail operations.",
        confidence=0.9,
        human_review_required=False,
    )
    factory.storage[conclusion.id] = conclusion
    statement = Statement(
        id=uuid4(),
        run_id=run.id,
        passage_id=uuid4(),
        text="AI adoption is growing in retail.",
        status="verified",
    )
    factory.storage[statement.id] = statement
    factory.storage[(conclusion.id, statement.id)] = ConclusionEvidence(
        conclusion_id=conclusion.id, statement_id=statement.id
    )
    response = client.get(f"/v1/runs/{run.id}/conclusions")
    assert response.status_code == 200
    conclusions = response.json()
    assert len(conclusions) == 1
    assert conclusions[0]["id"] == str(conclusion.id)
    assert conclusions[0]["text"] == "AI transforms retail operations."
    assert [entry["statement_id"] for entry in conclusions[0]["evidence"]] == [str(statement.id)]


# --------------------------------------------------------------------------- #
# POST /v1/runs/{id}/resume
# --------------------------------------------------------------------------- #
def test_resume_invokes_resume_runner(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id, status="paused", stage="extract", progress=0.5)
    runner = RecordingRunner()
    app.dependency_overrides[get_runner] = lambda: runner
    response = client.post(f"/v1/runs/{run.id}/resume")
    assert response.status_code == 200
    assert runner.resume_calls == [run.id]
    assert response.json()["status"] == "paused"  # fake runner does not mutate the row


# --------------------------------------------------------------------------- #
# Cross-tenant isolation (403, never 404 — RLS hook)
# --------------------------------------------------------------------------- #
def test_cross_tenant_get_run_returns_403(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage, namespace="default")
    tenant_a = seed_tenant(factory.storage, namespace="tenant-a")
    run = seed_run(factory.storage, tenant_a.id)
    response = client.get(f"/v1/runs/{run.id}", headers={"X-Tenant-ID": "default"})
    assert response.status_code == 403


def test_cross_tenant_resume_returns_403(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage, namespace="default")
    tenant_a = seed_tenant(factory.storage, namespace="tenant-a")
    run = seed_run(factory.storage, tenant_a.id, status="paused")
    runner = RecordingRunner()
    app.dependency_overrides[get_runner] = lambda: runner
    response = client.post(f"/v1/runs/{run.id}/resume", headers={"X-Tenant-ID": "default"})
    assert response.status_code == 403
    assert runner.resume_calls == []


# --------------------------------------------------------------------------- #
# Evaluator flow end-to-end (submit -> poll -> conclusions -> trace)
# --------------------------------------------------------------------------- #
def test_evaluator_flow_end_to_end(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    runner = CompletingRunner(factory)
    app.dependency_overrides[get_runner] = lambda: runner

    created = client.post(
        "/v1/runs", json={"question": "How is AI transforming retail?", "execute": True}
    )
    assert created.status_code == 201
    run_id = created.json()["run_id"]

    polled = client.get(f"/v1/runs/{run_id}")
    assert polled.status_code == 200
    assert polled.json()["status"] == "completed"
    assert polled.json()["progress"] == 1.0

    conclusions = client.get(f"/v1/runs/{run_id}/conclusions")
    assert conclusions.status_code == 200
    body = conclusions.json()
    assert len(body) == 1
    statement_id = body[0]["evidence"][0]["statement_id"]

    traced = client.get(f"/v1/statements/{statement_id}/trace")
    assert traced.status_code == 200
    chain = traced.json()
    assert set(chain) == {"statement", "passage", "source"}
    assert chain["statement"]["id"] == statement_id
    assert chain["source"]["uri"] == "https://retail.example.com/report"
    assert chain["source"]["title"] == "Retail report"
