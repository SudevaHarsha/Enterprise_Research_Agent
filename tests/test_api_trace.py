"""API tests: provenance trace, contradictions, report rendering (task_012).

Hermetic: TestClient + dependency overrides + FakeSession. Covers the brief's
trace-surface cases: ``GET /v1/statements/{id}/trace`` resolves exactly
statement -> passage -> source (3 nodes, direct FK hops only), contradictions
are returned for a run, and the report endpoint serves markdown from the
``trace:{run_id}`` kv_cache artifact, falling back to the conclude checkpoint,
then to a deterministic render from conclusion rows (no LLM in the API).
G-05 redaction is asserted on trace texts and report topics.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session_factory
from app.db.models import (
    Checkpoint,
    Conclusion,
    ConclusionEvidence,
    Contradiction,
    KVEntry,
    Passage,
    Run,
    Source,
    Statement,
    Tenant,
)
from app.main import app
from app.services.report_renderer import Report
from tests.conftest import FakeSessionFactory

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


def seed_tenant(storage: dict[Any, Any], namespace: str = "default") -> Tenant:
    tenant = Tenant(id=uuid4(), name=namespace.title(), namespace=namespace, rbac_policy={})
    storage[tenant.id] = tenant
    return tenant


def seed_run(storage: dict[Any, Any], tenant_id: UUID) -> Run:
    run = Run(
        id=uuid4(),
        tenant_id=tenant_id,
        question="How is AI transforming retail operations?",
        status="completed",
        stage="done",
        progress=1.0,
        created_at=datetime.now(UTC),
    )
    storage[run.id] = run
    return run


def seed_chain(
    storage: dict[Any, Any],
    run: Run,
    *,
    statement_text: str = "AI adoption is growing in retail.",
) -> tuple[Statement, Passage, Source]:
    """Insert one statement -> passage -> source chain owned by ``run``."""
    source = Source(
        id=uuid4(),
        run_id=run.id,
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
        run_id=run.id,
        passage_id=passage.id,
        text=statement_text,
        status="verified",
        confidence=0.9,
    )
    storage[source.id] = source
    storage[passage.id] = passage
    storage[statement.id] = statement
    return statement, passage, source


@pytest.fixture
def api() -> Iterator[tuple[TestClient, FakeSessionFactory]]:
    app.dependency_overrides.clear()
    factory = FakeSessionFactory()
    app.dependency_overrides[get_session_factory] = lambda: factory
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# GET /v1/statements/{id}/trace
# --------------------------------------------------------------------------- #
def test_trace_resolves_statement_passage_source_exactly_three_nodes(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    statement, passage, source = seed_chain(factory.storage, run)

    response = client.get(f"/v1/statements/{statement.id}/trace")
    assert response.status_code == 200
    chain = response.json()
    # exactly 3 nodes: statement -> passage -> source (<=1 hop per edge)
    assert set(chain) == {"statement", "passage", "source"}
    assert chain["statement"]["id"] == str(statement.id)
    assert chain["statement"]["kind"] == "statement"
    assert chain["statement"]["text"] == "AI adoption is growing in retail."
    assert chain["passage"]["id"] == str(passage.id)
    assert chain["passage"]["kind"] == "passage"
    assert chain["passage"]["text"] == "Retailers adopt AI for inventory management."
    assert chain["source"]["id"] == str(source.id)
    assert chain["source"]["kind"] == "source"
    assert chain["source"]["uri"] == "https://retail.example.com/report"
    assert chain["source"]["title"] == "Retail report"


def test_trace_redacts_secret_looking_text(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    statement, _, _ = seed_chain(
        factory.storage, run, statement_text=f"AI adoption is growing with {SECRET}."
    )
    response = client.get(f"/v1/statements/{statement.id}/trace")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "AI adoption is growing" in response.json()["statement"]["text"]


def test_trace_404_unknown_statement(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage)
    response = client.get(f"/v1/statements/{uuid4()}/trace")
    assert response.status_code == 404


def test_trace_cross_tenant_returns_403(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage, namespace="default")
    tenant_a = seed_tenant(factory.storage, namespace="tenant-a")
    run = seed_run(factory.storage, tenant_a.id)
    statement, _, _ = seed_chain(factory.storage, run)
    response = client.get(
        f"/v1/statements/{statement.id}/trace", headers={"X-Tenant-ID": "default"}
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/contradictions
# --------------------------------------------------------------------------- #
def test_contradictions_returns_records(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    contradiction = Contradiction(
        id=uuid4(),
        run_id=run.id,
        statement_a_id=uuid4(),
        statement_b_id=uuid4(),
        status="confirmed",
        evidence={"summary": "conflicting claims"},
    )
    factory.storage[contradiction.id] = contradiction
    response = client.get(f"/v1/runs/{run.id}/contradictions")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(contradiction.id)
    assert body[0]["statement_a_id"] == str(contradiction.statement_a_id)
    assert body[0]["statement_b_id"] == str(contradiction.statement_b_id)
    assert body[0]["status"] == "confirmed"
    assert body[0]["evidence"] == {"summary": "conflicting claims"}


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/report
# --------------------------------------------------------------------------- #
def test_report_renders_markdown_from_trace_artifact(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    report = Report(
        run_id=str(run.id),
        topic="AI retail transformation",
        generated_at=datetime.now(UTC),
        conclusions=[],
    )
    factory.storage[f"trace:{run.id}"] = KVEntry(
        key=f"trace:{run.id}",
        model="pipeline/trace",
        prompt_hash="trace-stage",
        payload=report.model_dump(mode="json"),
    )
    response = client.get(f"/v1/runs/{run.id}/report")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == str(run.id)
    assert "# AI retail transformation" in body["markdown"]
    assert body["markdown"].startswith("# ")


def test_report_falls_back_to_conclude_checkpoint(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    report = Report(
        run_id=str(run.id),
        topic="Checkpoint topic",
        generated_at=datetime.now(UTC),
        conclusions=[],
    )
    factory.storage[uuid4()] = Checkpoint(
        id=uuid4(),
        run_id=run.id,
        stage="conclude",
        state={"report": report.model_dump(mode="json")},
        ts=datetime.now(UTC),
    )
    response = client.get(f"/v1/runs/{run.id}/report")
    assert response.status_code == 200
    assert "# Checkpoint topic" in response.json()["markdown"]


def test_report_renders_from_rows_when_no_artifacts(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    statement, _, source = seed_chain(factory.storage, run)
    conclusion = Conclusion(
        id=uuid4(),
        run_id=run.id,
        text="AI is transforming retail operations.",
        confidence=0.9,
        human_review_required=False,
    )
    factory.storage[conclusion.id] = conclusion
    factory.storage[(conclusion.id, statement.id)] = ConclusionEvidence(
        conclusion_id=conclusion.id, statement_id=statement.id
    )
    response = client.get(f"/v1/runs/{run.id}/report")
    assert response.status_code == 200
    markdown = response.json()["markdown"]
    assert "AI is transforming retail operations." in markdown
    assert "retail.example.com" in markdown  # reference from source URI


def test_report_redacts_secret_topic(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    report = Report(
        run_id=str(run.id),
        topic=f"AI retail with {SECRET}",
        generated_at=datetime.now(UTC),
        conclusions=[],
    )
    factory.storage[f"trace:{run.id}"] = KVEntry(
        key=f"trace:{run.id}",
        model="pipeline/trace",
        prompt_hash="trace-stage",
        payload=report.model_dump(mode="json"),
    )
    response = client.get(f"/v1/runs/{run.id}/report")
    assert response.status_code == 200
    assert SECRET not in response.text
