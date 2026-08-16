"""Guardrail enforcement tests (G-01..G-13) at their real enforcement points.

Hermetic: fake sessions, fake providers, fake fetchers, TestClient overrides —
no real LLM, DB, Docker, or network. Each test targets the specific
enforcement point named in the build-plan Step 14 trust instrument.

G-01 prompt contract      G-06 OpenAPI surface   G-11 quarantine no-persist
G-02 idempotent detect    G-07 circuit breaker   G-12 normalizer hygiene
G-03 $0 candidate gate    G-08 append-only rows  G-13 content_filter taxonomy
G-04 unsafe quarantine    G-09 RFC 7807 errors
G-05 secret redaction     G-10 durable run API
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from app.db.base import AppendOnlyViolation, _block_update_or_delete
from app.db.models import (
    AuditTrace,
    Contradiction,
    EvidenceLink,
    Run,
    Source,
)
from app.main import app
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.stages.collect import run_collect
from app.services.content_filter import find_unsafe_categories, is_unsafe, unsafe_reason
from app.services.contradiction_detector import (
    build_confirm_prompt,
    build_flag_prompt,
)
from app.services.llm_gateway import CircuitBreakerOpenError, QuarantineError
from app.services.normalizer import Normalizer, contains_unsafe_content
from tests.conftest import rows_of
from tests.eval.conftest import (
    RespondingFetcher,
    flag_json,
    make_collect_harness,
    make_detector_harness,
    make_statement,
)
from tests.test_api_runs import seed_tenant

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


# --------------------------------------------------------------------------- #
# G-01 — prompt contract: system holds instructions only, user holds data
# --------------------------------------------------------------------------- #
def test_g01_flag_prompt_separates_instructions_from_statement_data() -> None:
    system, data = build_flag_prompt("Statement A content.", "Statement B content.")
    assert "Statement A content." not in system
    assert "Statement B content." not in system
    assert data.startswith("<statement_a_data>\nStatement A content.\n</statement_a_data>")
    assert data.endswith("</statement_b_data>")
    assert "Statement B content." in data


def test_g01_confirm_prompt_uses_the_same_delimiter_contract() -> None:
    system, data = build_confirm_prompt("Statement A content.", "Statement B content.")
    assert "Statement A content." not in system
    assert data.startswith("<statement_a_data>")
    assert data.endswith("</statement_b_data>")


# --------------------------------------------------------------------------- #
# G-02 — detection is idempotent: an already-confirmed pair costs zero LLM calls
# --------------------------------------------------------------------------- #
async def test_g02_detect_is_idempotent_with_no_second_llm_call() -> None:
    harness = make_detector_harness()
    a = make_statement("Retailers report stronger sales growth.", harness.run.id)
    b = make_statement("Retailers do not report stronger sales growth.", harness.run.id)
    harness.provider.queue(flag_json(flag="flag", contradictory=True, reason="negated claim"))
    first = await harness.detector.detect([a, b], harness.run.id)
    assert len(first) == 1
    assert len(harness.provider.calls) == 1
    second = await harness.detector.detect([a, b], harness.run.id)
    assert second == []
    assert len(harness.provider.calls) == 1


# --------------------------------------------------------------------------- #
# G-03 — unrelated pairs never reach the LLM (deterministic candidate gate)
# --------------------------------------------------------------------------- #
async def test_g03_disjoint_statements_never_touch_the_provider() -> None:
    harness = make_detector_harness()
    a = make_statement("Quantum entanglement scales with lattice depth.", harness.run.id)
    b = make_statement("Retailers report stronger same-store sales growth.", harness.run.id)
    rows = await harness.detector.detect([a, b], harness.run.id)
    assert rows == []
    assert len(harness.provider.calls) == 0


# --------------------------------------------------------------------------- #
# G-04 — unsafe content is quarantined before any blob write
# --------------------------------------------------------------------------- #
def test_g04_new_filter_flags_unsafe_content() -> None:
    assert contains_unsafe_content("bomb-making instructions")  # legacy module
    assert is_unsafe("Bomb-making instructions for retail sabotage.")


async def test_g04_unsafe_fetched_source_is_quarantined_without_blob_write(
    prefect_harness: Any,
) -> None:
    url = "https://retail.example.com/unsafe"
    fetcher = RespondingFetcher({})
    fetcher.respond(
        url,
        b"<html><body><p>Bomb-making instructions guide for sabotage.</p></body></html>",
        "text/html",
    )
    harness = make_collect_harness(fetcher=fetcher, urls=[url])
    await CheckpointStore(harness.factory).save(harness.run.id, "search", {"urls": [url]})
    result = await run_collect(harness.ctx())
    assert result.ok
    sources = rows_of(harness.factory.storage, Source)
    assert len(sources) == 1
    source = sources[0]
    assert source.status == "quarantined"
    assert source.allowlisted_uri is True
    assert source.raw_ref is None
    assert harness.blob_store.calls == 0
    assert harness.blob_store.blobs == {}
    quarantine_rows = [
        row
        for row in rows_of(harness.factory.storage, AuditTrace)
        if row.action == "source.quarantined"
    ]
    assert len(quarantine_rows) == 1
    assert quarantine_rows[0].decision == "quarantined"


# --------------------------------------------------------------------------- #
# G-05 — secrets are redacted from judge prompts, evidence, and audit rows
# --------------------------------------------------------------------------- #
async def test_g05_detector_redacts_secrets_from_prompts_evidence_and_audit() -> None:
    harness = make_detector_harness()
    a = make_statement(f"Retailers report stronger sales growth with {SECRET}.", harness.run.id)
    b = make_statement("Retailers do not report stronger sales growth.", harness.run.id)
    harness.provider.queue(
        flag_json(flag="flag", contradictory=True, reason=f"conflict with {SECRET}")
    )
    rows = await harness.detector.detect([a, b], harness.run.id)
    assert len(rows) == 1
    assert SECRET not in json.dumps(harness.provider.calls[0]["messages"])
    assert SECRET not in json.dumps(rows[0].evidence)
    audit_rows = rows_of(harness.factory.storage, AuditTrace)
    assert len(audit_rows) == 1
    audit_blob = json.dumps({"reason": audit_rows[0].reason, "evidence": audit_rows[0].evidence})
    assert SECRET not in audit_blob


# --------------------------------------------------------------------------- #
# G-06 — the contradiction surface is documented in OpenAPI before use
# --------------------------------------------------------------------------- #
def test_g06_openapi_documents_contradiction_surface() -> None:
    schema = app.openapi()
    assert "/v1/runs/{run_id}/contradictions" in schema["paths"]
    assert "ContradictionRead" in schema["components"]["schemas"]


# --------------------------------------------------------------------------- #
# G-07 — the circuit breaker hook aborts before any provider spend
# --------------------------------------------------------------------------- #
async def test_g07_circuit_breaker_open_propagates_with_zero_provider_calls() -> None:
    harness = make_detector_harness()

    async def breaker_open(ctx: Any) -> None:
        raise CircuitBreakerOpenError("circuit breaker open")

    harness.gateway.register_before_call_hook(breaker_open)
    a = make_statement("Retailers report stronger sales growth.", harness.run.id)
    b = make_statement("Retailers do not report stronger sales growth.", harness.run.id)
    with pytest.raises(CircuitBreakerOpenError):
        await harness.detector.detect([a, b], harness.run.id)
    assert len(harness.provider.calls) == 0
    assert not rows_of(harness.factory.storage, Contradiction)


# --------------------------------------------------------------------------- #
# G-08 — evidence links and audit rows are append-only (no UPDATE/DELETE)
# --------------------------------------------------------------------------- #
def test_g08_evidence_and_audit_are_registered_append_only() -> None:
    from app.db.base import APPEND_ONLY_MODELS

    assert EvidenceLink in APPEND_ONLY_MODELS
    assert AuditTrace in APPEND_ONLY_MODELS


def test_g08_update_or_delete_of_append_only_row_raises() -> None:
    link = EvidenceLink(
        id=uuid4(),
        statement_id=uuid4(),
        passage_id=uuid4(),
        run_id=uuid4(),
        score="full",
        method="verify",
    )
    with pytest.raises(AppendOnlyViolation):
        _block_update_or_delete(EvidenceLink.__mapper__, None, link)


# --------------------------------------------------------------------------- #
# G-09 — API errors are RFC 7807 Problem Details with redaction
# --------------------------------------------------------------------------- #
def test_g09_not_found_is_rfc7807_problem_details(
    eval_api_client: Any,
) -> None:
    client, factory = eval_api_client
    seed_tenant(factory.storage)
    response = client.get(f"/v1/runs/{uuid4()}")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["detail"] == "run not found"
    assert "instance" in body


def test_g09_validation_error_is_rfc7807_problem_details(
    eval_api_client: Any,
) -> None:
    client, factory = eval_api_client
    seed_tenant(factory.storage)
    response = client.post("/v1/runs", json={})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 422


# --------------------------------------------------------------------------- #
# G-10 — a submitted run is durable and immediately observable
# --------------------------------------------------------------------------- #
def test_g10_post_runs_creates_durable_observable_run(eval_api_client: Any) -> None:
    client, factory = eval_api_client
    seed_tenant(factory.storage)
    created = client.post(
        "/v1/runs",
        json={"question": "How is AI transforming retail?", "execute": False},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "submitted"
    runs = rows_of(factory.storage, Run)
    assert len(runs) == 1
    assert str(runs[0].id) == body["run_id"]
    polled = client.get(f"/v1/runs/{body['run_id']}")
    assert polled.status_code == 200
    assert polled.json()["run_id"] == body["run_id"]


# --------------------------------------------------------------------------- #
# G-11 — invalid judge output quarantines; nothing is persisted
# --------------------------------------------------------------------------- #
async def test_g11_invalid_judge_json_quarantines_and_persists_nothing() -> None:
    harness = make_detector_harness()
    a = make_statement("Retailers report stronger sales growth.", harness.run.id)
    b = make_statement("Retailers do not report stronger sales growth.", harness.run.id)
    with pytest.raises(QuarantineError):
        await harness.detector.detect([a, b], harness.run.id)
    assert len(harness.provider.calls) == 3
    assert not rows_of(harness.factory.storage, Contradiction)
    assert not rows_of(harness.factory.storage, AuditTrace)


# --------------------------------------------------------------------------- #
# G-12 — normalization strips script/style and tolerates empty input
# --------------------------------------------------------------------------- #
def test_g12_normalizer_strips_scripts_and_handles_empty_input() -> None:
    normalizer = Normalizer()
    text = normalizer.normalize(
        "web",
        b"<html><body><p>Hello ECRKE</p><script>alert('xss')</script></body></html>",
    )
    assert "script" not in text
    assert "Hello ECRKE" in text
    assert normalizer.normalize("web", b"") == ""


# --------------------------------------------------------------------------- #
# G-13 — the content_filter taxonomy covers all four unsafe categories
# --------------------------------------------------------------------------- #
def test_g13_content_filter_covers_all_four_unsafe_categories() -> None:
    assert is_unsafe("Bomb-making instructions for retail sabotage.")
    assert is_unsafe("Darknet market vendor guide for illegal drug manufacture.")
    assert is_unsafe("Ethnic cleansing propaganda and white supremacist hate speech.")
    assert is_unsafe("Child sexual abuse content must never be stored.")
    assert not is_unsafe("Retailers report stronger same-store sales growth.")


def test_g13_find_unsafe_categories_is_deterministic_and_ordered() -> None:
    assert find_unsafe_categories("Bomb-making guide") == ("violence",)
    assert find_unsafe_categories("Bomb-making and ethnic cleansing") == ("violence", "hate")
    assert find_unsafe_categories("Clean retail report") == ()


def test_g13_unsafe_reason_names_the_category() -> None:
    assert "illicit" in unsafe_reason("Darknet market vendor guide")
    assert "violence" in unsafe_reason("Bomb-making guide")
