"""API tests: KB search and audit export (task_012).

Hermetic: TestClient + dependency overrides + FakeSession. Covers the brief's
knowledge-surface cases: ``GET /v1/kb/search`` returns verified statements
across runs (tenant-scoped) with an optional ``?q=`` text filter and an
evidence summary; ``GET /v1/runs/{id}/audit`` exports the immutable audit
trace redacted via ``redact_json`` (G-05) so a fake secret never appears in
the response.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session_factory
from app.db.models import (
    AuditTrace,
    Passage,
    Run,
    Source,
    Statement,
    Tenant,
)
from app.main import app
from tests.conftest import FakeSessionFactory

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


def seed_tenant(storage: dict[Any, Any], namespace: str = "default") -> Tenant:
    tenant = Tenant(id=uuid4(), name=namespace.title(), namespace=namespace, rbac_policy={})
    storage[tenant.id] = tenant
    return tenant


def seed_run(storage: dict[Any, Any], tenant_id: Any, *, question: str = "AI retail") -> Run:
    run = Run(
        id=uuid4(),
        tenant_id=tenant_id,
        question=question,
        status="completed",
        stage="done",
        progress=1.0,
        created_at=datetime.now(UTC),
    )
    storage[run.id] = run
    return run


def seed_verified_statement(
    storage: dict[Any, Any],
    run: Run,
    *,
    text: str,
    status: str = "verified",
    uri: str = "https://retail.example.com/report",
) -> Statement:
    source = Source(
        id=uuid4(),
        run_id=run.id,
        uri=uri,
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
        text=text,
        status=status,
        confidence=0.9,
    )
    storage[source.id] = source
    storage[passage.id] = passage
    storage[statement.id] = statement
    return statement


@pytest.fixture
def api() -> Iterator[tuple[TestClient, FakeSessionFactory]]:
    app.dependency_overrides.clear()
    factory = FakeSessionFactory()
    app.dependency_overrides[get_session_factory] = lambda: factory
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# GET /v1/kb/search
# --------------------------------------------------------------------------- #
def test_kb_search_filters_verified_statements_by_query(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    seed_verified_statement(factory.storage, run, text="AI transforms retail logistics.")
    seed_verified_statement(factory.storage, run, text="Warehouse automation improves throughput.")
    seed_verified_statement(factory.storage, run, text="AI retail is unverified.", status="draft")

    response = client.get("/v1/kb/search", params={"q": "retail"})
    assert response.status_code == 200
    body = response.json()
    assert [entry["text"] for entry in body] == ["AI transforms retail logistics."]
    assert body[0]["source_uri"] == "https://retail.example.com/report"
    assert body[0]["passage_text"] == "Retailers adopt AI for inventory management."
    assert body[0]["confidence"] == 0.9


def test_kb_search_returns_all_verified_without_query(
    api: tuple[TestClient, FakeSessionFactory],
) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    seed_verified_statement(factory.storage, run, text="AI transforms retail logistics.")
    seed_verified_statement(factory.storage, run, text="Warehouse automation improves throughput.")
    seed_verified_statement(factory.storage, run, text="AI retail is unverified.", status="draft")

    response = client.get("/v1/kb/search")
    assert response.status_code == 200
    body = response.json()
    assert {entry["text"] for entry in body} == {
        "AI transforms retail logistics.",
        "Warehouse automation improves throughput.",
    }


def test_kb_search_is_tenant_scoped(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant_a = seed_tenant(factory.storage, namespace="default")
    tenant_b = seed_tenant(factory.storage, namespace="tenant-b")
    run_a = seed_run(factory.storage, tenant_a.id)
    run_b = seed_run(factory.storage, tenant_b.id)
    seed_verified_statement(factory.storage, run_a, text="Only tenant A sees this.")
    seed_verified_statement(factory.storage, run_b, text="Only tenant B sees this.")

    response = client.get("/v1/kb/search", headers={"X-Tenant-ID": "default"})
    assert response.status_code == 200
    body = response.json()
    assert [entry["text"] for entry in body] == ["Only tenant A sees this."]


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/audit
# --------------------------------------------------------------------------- #
def test_audit_exports_redacted_rows(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    tenant = seed_tenant(factory.storage)
    run = seed_run(factory.storage, tenant.id)
    now = datetime.now(UTC)
    factory.storage[uuid4()] = AuditTrace(
        id=uuid4(),
        run_id=run.id,
        entity_type="statement",
        entity_id="stmt-1",
        action="statement.verify",
        actor="pipeline",
        decision="verified",
        reason="matrix full",
        evidence={"llm_output": f"claim uses {SECRET}"},
        ts=now,
    )
    factory.storage[uuid4()] = AuditTrace(
        id=uuid4(),
        run_id=run.id,
        entity_type="source",
        entity_id="src-1",
        action="source.fetched",
        actor="pipeline",
        decision="fetched",
        reason="allowlisted",
        evidence=None,
        ts=now,
    )
    response = client.get(f"/v1/runs/{run.id}/audit")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["run_id"] == str(run.id)
    assert len(body["rows"]) == 2
    actions = {row["action"] for row in body["rows"]}
    assert actions == {"statement.verify", "source.fetched"}
    # G-05: the fake secret never survives redaction
    assert SECRET not in response.text


def test_audit_cross_tenant_returns_403(api: tuple[TestClient, FakeSessionFactory]) -> None:
    client, factory = api
    seed_tenant(factory.storage, namespace="default")
    tenant_a = seed_tenant(factory.storage, namespace="tenant-a")
    run = seed_run(factory.storage, tenant_a.id)
    response = client.get(f"/v1/runs/{run.id}/audit", headers={"X-Tenant-ID": "default"})
    assert response.status_code == 403
