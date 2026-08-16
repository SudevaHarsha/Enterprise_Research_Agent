"""Evaluator-facing HTTP surface — the ten ``/v1`` endpoints (task_012).

Composes Phase-1 services only; the API never calls an LLM provider directly.
Every text field is G-05 redacted before it reaches a response model, and every
run-scoped endpoint enforces tenant isolation via ``X-Tenant-ID`` (403 on
cross-tenant access). Error responses are RFC 7807 Problem Details, rendered by
the exception handlers registered in ``app.main``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, cast
from urllib.parse import urlparse
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import PipelineRunner, get_runner, get_session_factory, get_settings_dep
from app.api.schemas import (
    AuditExport,
    AuditRow,
    ConclusionRead,
    ContradictionRead,
    EvidenceRef,
    KBSearchResult,
    ReportRead,
    RunCreate,
    RunRead,
    StageInfo,
    TraceChain,
    TraceNode,
)
from app.api.tenant import get_tenant_id
from app.core.config import Settings
from app.db.enums import RunStatus
from app.db.models import (
    AuditTrace,
    Checkpoint,
    Conclusion,
    ConclusionEvidence,
    Contradiction,
    EvidenceLink,
    Passage,
    Run,
    Source,
    Statement,
)
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.context import STAGES, SessionFactory
from app.services.audit_writer import redact_json
from app.services.kv_cache import KVCache
from app.services.normalizer import redact_secrets
from app.services.report_renderer import (
    Report,
    ReportConclusion,
    ReportEvidenceStatement,
    ReportSupportEntry,
    render_markdown,
)

router = APIRouter(prefix="/v1", tags=["evaluator"])


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _domain(uri: str) -> str:
    """Return the netloc of ``uri`` (source-domain label for report references)."""
    return urlparse(uri).netloc or uri.split("/")[0]


def _run_to_read(run: Run) -> RunRead:
    """Map a Run row to the observable RunRead contract (question redacted)."""
    return RunRead(
        run_id=run.id,
        tenant_id=run.tenant_id,
        question=redact_secrets(run.question),
        status=run.status,
        stage=run.stage,
        progress=run.progress,
        cost_budget_usd=run.cost_budget_usd,
        cost_spent_usd=run.cost_spent_usd,
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


async def _load_run(session: AsyncSession, run_id: UUID, tenant_id: UUID) -> Run:
    """Load a run with tenant isolation: 404 when missing, 403 when not ours."""
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="cross-tenant access denied")
    return run


def _report_from_trace_payload(payload: dict[str, Any] | None) -> Report | None:
    """Extract a Report from the ``trace:{run_id}`` kv_cache artifact.

    Production trace stages store ``{"report": {...}}``; the API also accepts a
    bare ``Report`` dump (tests and older artifacts).
    """
    if payload is None:
        return None
    candidate = payload.get("report")
    if isinstance(candidate, dict):
        return Report.model_validate(candidate)
    try:
        return Report.model_validate(payload)
    except ValidationError:
        return None


async def _report_from_rows(session: AsyncSession, run: Run) -> Report:
    """Deterministically render a report from conclusion rows (no LLM).

    Fallback when neither the trace artifact nor the conclude checkpoint
    exists: conclusions carry their evidence statements with source domains,
    matching the public report contract.
    """
    conclusions = list(await session.scalars(select(Conclusion).where(Conclusion.run_id == run.id)))
    if not conclusions:
        return Report(
            run_id=str(run.id),
            topic=redact_secrets(run.question),
            generated_at=datetime.now(UTC),
        )
    conclusion_ids = [row.id for row in conclusions]
    links = list(
        await session.scalars(
            select(ConclusionEvidence).where(ConclusionEvidence.conclusion_id.in_(conclusion_ids))
        )
    )
    statement_ids = sorted({link.statement_id for link in links})
    statements: dict[UUID, Statement] = {}
    if statement_ids:
        statements = {
            row.id: row
            for row in await session.scalars(
                select(Statement).where(Statement.id.in_(statement_ids))
            )
        }
    passage_ids = sorted({statement.passage_id for statement in statements.values()})
    passages: dict[UUID, Passage] = {}
    if passage_ids:
        passages = {
            row.id: row
            for row in await session.scalars(select(Passage).where(Passage.id.in_(passage_ids)))
        }
    source_ids = sorted({passage.source_id for passage in passages.values()})
    sources: dict[UUID, Source] = {}
    if source_ids:
        sources = {
            row.id: row
            for row in await session.scalars(select(Source).where(Source.id.in_(source_ids)))
        }
    evidence_scores = {
        (link.statement_id, link.passage_id): link.score
        for link in await session.scalars(select(EvidenceLink).where(EvidenceLink.run_id == run.id))
    }
    report_conclusions: list[ReportConclusion] = []
    for conclusion in conclusions:
        support_matrix: list[ReportSupportEntry] = []
        evidence_statements: list[ReportEvidenceStatement] = []
        for link in links:
            if link.conclusion_id != conclusion.id:
                continue
            statement = statements.get(link.statement_id)
            if statement is None:
                continue
            passage = passages.get(statement.passage_id)
            source = sources.get(passage.source_id) if passage is not None else None
            score = evidence_scores.get((statement.id, statement.passage_id), "none")
            support_matrix.append(
                ReportSupportEntry(
                    statement_id=str(statement.id),
                    passage_id=str(statement.passage_id),
                    support_score=score,
                )
            )
            evidence_statements.append(
                ReportEvidenceStatement(
                    id=str(statement.id),
                    text=redact_secrets(statement.text),
                    source_domain=_domain(source.uri) if source is not None else "",
                )
            )
        report_conclusions.append(
            ReportConclusion(
                id=str(conclusion.id),
                text=redact_secrets(conclusion.text),
                confidence=conclusion.confidence,
                human_review_required=conclusion.human_review_required,
                support_matrix=support_matrix,
                evidence_statements=evidence_statements,
            )
        )
    return Report(
        run_id=str(run.id),
        topic=redact_secrets(run.question),
        generated_at=datetime.now(UTC),
        conclusions=report_conclusions,
    )


# --------------------------------------------------------------------------- #
# POST /v1/runs — submit (and optionally execute) a research question
# --------------------------------------------------------------------------- #
@router.post(
    "/runs",
    status_code=status.HTTP_201_CREATED,
    response_model=RunRead,
    summary="Submit a research question",
)
async def create_run(
    body: RunCreate,
    runner: Annotated[PipelineRunner, Depends(get_runner)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> RunRead:
    """Create a submitted run; when ``execute`` is true, run the pipeline."""
    run = Run(
        id=uuid4(),
        tenant_id=tenant_id,
        question=body.question,
        status=RunStatus.SUBMITTED.value,
        stage=None,
        progress=0.0,
        cost_budget_usd=(
            body.cost_budget_usd
            if body.cost_budget_usd is not None
            else Decimal(str(settings.run_budget_usd))
        ),
        cost_spent_usd=Decimal("0.0000"),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        completed_at=None,
    )
    async with session_factory() as session:
        session.add(run)
        await session.commit()
    if body.execute:
        await runner.run(run.id, None)
    async with session_factory() as session:
        fresh = await session.get(Run, run.id)
    if fresh is None:
        raise HTTPException(status_code=500, detail="run created but could not be re-read")
    return _run_to_read(fresh)


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id} — poll one run
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}",
    response_model=RunRead,
    summary="Get run lifecycle state",
)
async def get_run(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> RunRead:
    """Return the observable lifecycle of one run (poll target)."""
    async with session_factory() as session:
        run = await _load_run(session, run_id, tenant_id)
        return _run_to_read(run)


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/stages — durable checkpoint stages
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}/stages",
    response_model=list[StageInfo],
    summary="List per-stage checkpoint info",
)
async def get_stages(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> list[StageInfo]:
    """Return one entry per durable checkpoint, ordered by pipeline stage."""
    async with session_factory() as session:
        await _load_run(session, run_id, tenant_id)
        rows = list(await session.scalars(select(Checkpoint).where(Checkpoint.run_id == run_id)))
    rows.sort(key=lambda row: STAGES.index(row.stage) if row.stage in STAGES else len(STAGES))
    return [StageInfo(stage=row.stage, ts=row.ts, summary=redact_json(row.state)) for row in rows]


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/conclusions — final conclusions with evidence links
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}/conclusions",
    response_model=list[ConclusionRead],
    summary="List run conclusions with evidence",
)
async def get_conclusions(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> list[ConclusionRead]:
    """Return the run's conclusions, each with its cited statement evidence."""
    async with session_factory() as session:
        await _load_run(session, run_id, tenant_id)
        rows = list(await session.scalars(select(Conclusion).where(Conclusion.run_id == run_id)))
        rows.sort(key=lambda row: row.created_at.isoformat() if row.created_at else "")
        result: list[ConclusionRead] = []
        for conclusion in rows:
            links = list(
                await session.scalars(
                    select(ConclusionEvidence).where(
                        ConclusionEvidence.conclusion_id == conclusion.id
                    )
                )
            )
            evidence = [
                EvidenceRef(statement_id=link.statement_id, finding_id=link.finding_id)
                for link in links
            ]
            result.append(
                ConclusionRead(
                    id=conclusion.id,
                    text=redact_secrets(conclusion.text),
                    confidence=conclusion.confidence,
                    human_review_required=conclusion.human_review_required,
                    evidence=evidence,
                )
            )
        return result


# --------------------------------------------------------------------------- #
# GET /v1/statements/{id}/trace — provenance chain (statement -> passage -> source)
# --------------------------------------------------------------------------- #
@router.get(
    "/statements/{statement_id}/trace",
    response_model=TraceChain,
    summary="Trace a statement to its source",
)
async def get_statement_trace(
    statement_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> TraceChain:
    """Resolve statement -> passage -> source in at most one hop per edge."""
    async with session_factory() as session:
        statement = await session.get(Statement, statement_id)
        if statement is None:
            raise HTTPException(status_code=404, detail="statement not found")
        run = await session.get(Run, statement.run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="statement not found")
        if run.tenant_id != tenant_id:
            raise HTTPException(status_code=403, detail="cross-tenant access denied")
        passage = await session.get(Passage, statement.passage_id)
        if passage is None:
            raise HTTPException(status_code=404, detail="passage not found")
        source = await session.get(Source, passage.source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
    return TraceChain(
        statement=TraceNode(id=statement.id, kind="statement", text=redact_secrets(statement.text)),
        passage=TraceNode(id=passage.id, kind="passage", text=redact_secrets(passage.text)),
        source=TraceNode(
            id=source.id,
            kind="source",
            uri=redact_secrets(source.uri),
            title=redact_secrets(source.title) if source.title is not None else None,
        ),
    )


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/contradictions
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}/contradictions",
    response_model=list[ContradictionRead],
    summary="List flagged/confirmed contradictions",
)
async def get_contradictions(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> list[ContradictionRead]:
    """Return the run's contradiction records with redacted evidence."""
    async with session_factory() as session:
        await _load_run(session, run_id, tenant_id)
        rows = list(
            await session.scalars(select(Contradiction).where(Contradiction.run_id == run_id))
        )
    rows.sort(key=lambda row: row.created_at.isoformat() if row.created_at else "")
    return [
        ContradictionRead(
            id=row.id,
            statement_a_id=row.statement_a_id,
            statement_b_id=row.statement_b_id,
            status=row.status,
            evidence=redact_json(row.evidence),
            created_at=row.created_at,
        )
        for row in rows
    ]


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/report — rendered markdown (no LLM in the API)
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}/report",
    response_model=ReportRead,
    summary="Render the run report as markdown",
)
async def get_report(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> ReportRead:
    """Serve markdown: trace artifact -> conclude checkpoint -> rows fallback.

    Prefer the ``trace:{run_id}`` kv_cache report bundle; fall back to the
    conclude checkpoint; otherwise render deterministically from conclusion
    rows. No LLM calls happen in the API.
    """
    cache = KVCache(session_factory=cast(async_sessionmaker[AsyncSession], session_factory))
    checkpoint_store = CheckpointStore(session_factory)
    async with session_factory() as session:
        run = await _load_run(session, run_id, tenant_id)
        trace_payload = await cache.get(f"trace:{run_id}")
        if trace_payload is not None:
            report = _report_from_trace_payload(trace_payload)
            if report is not None:
                return ReportRead(run_id=run.id, markdown=redact_secrets(render_markdown(report)))
        conclude_state = await checkpoint_store.load(run_id, "conclude")
        if conclude_state is not None and isinstance(conclude_state.get("report"), dict):
            report = Report.model_validate(conclude_state["report"])
            return ReportRead(run_id=run.id, markdown=redact_secrets(render_markdown(report)))
        report = await _report_from_rows(session, run)
        return ReportRead(run_id=run.id, markdown=redact_secrets(render_markdown(report)))


# --------------------------------------------------------------------------- #
# POST /v1/runs/{id}/resume — re-enter the pipeline at the first missing stage
# --------------------------------------------------------------------------- #
@router.post(
    "/runs/{run_id}/resume",
    response_model=RunRead,
    summary="Resume a paused/failed run",
)
async def resume_run(
    run_id: UUID,
    runner: Annotated[PipelineRunner, Depends(get_runner)],
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> RunRead:
    """Re-enter the pipeline; checkpointed stages are skipped automatically."""
    async with session_factory() as session:
        run = await _load_run(session, run_id, tenant_id)
    await runner.resume(run.id, None)
    async with session_factory() as session:
        fresh = await session.get(Run, run.id)
    if fresh is None:
        return _run_to_read(run)
    return _run_to_read(fresh)


# --------------------------------------------------------------------------- #
# GET /v1/kb/search — tenant-scoped verified knowledge base
# --------------------------------------------------------------------------- #
@router.get(
    "/kb/search",
    response_model=list[KBSearchResult],
    summary="Search verified statements across the tenant's runs",
)
async def kb_search(
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
    q: Annotated[str | None, Query(max_length=500)] = None,
) -> list[KBSearchResult]:
    """Return verified statements for the tenant, optionally filtered by text.

    The SQL surface stays fake-friendly (single ``==``/``in_`` predicates);
    filtering by status and query happens in Python over the tenant's rows.
    """
    async with session_factory() as session:
        runs = list(await session.scalars(select(Run).where(Run.tenant_id == tenant_id)))
        run_ids = [run.id for run in runs]
        if not run_ids:
            return []
        statements = list(
            await session.scalars(select(Statement).where(Statement.run_id.in_(run_ids)))
        )
        verified = [row for row in statements if row.status == "verified"]
        query = (q or "").strip().lower()
        if query:
            verified = [row for row in verified if query in row.text.lower()]
        if not verified:
            return []
        passage_ids = sorted({row.passage_id for row in verified})
        passages = {
            row.id: row
            for row in await session.scalars(select(Passage).where(Passage.id.in_(passage_ids)))
        }
        source_ids = sorted({passage.source_id for passage in passages.values()})
        sources = {
            row.id: row
            for row in await session.scalars(select(Source).where(Source.id.in_(source_ids)))
        }
        results: list[KBSearchResult] = []
        for statement in verified:
            passage = passages.get(statement.passage_id)
            source = sources.get(passage.source_id) if passage is not None else None
            results.append(
                KBSearchResult(
                    statement_id=statement.id,
                    run_id=statement.run_id,
                    text=redact_secrets(statement.text),
                    confidence=statement.confidence,
                    passage_text=redact_secrets(passage.text) if passage is not None else None,
                    source_uri=redact_secrets(source.uri) if source is not None else None,
                )
            )
        return results


# --------------------------------------------------------------------------- #
# GET /v1/runs/{id}/audit — immutable audit trace (redacted)
# --------------------------------------------------------------------------- #
@router.get(
    "/runs/{run_id}/audit",
    response_model=AuditExport,
    summary="Export the run's audit trace",
)
async def get_audit(
    run_id: UUID,
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    tenant_id: Annotated[UUID, Depends(get_tenant_id)],
) -> AuditExport:
    """Export every ``audit_trace`` row for the run, redacted via G-05."""
    async with session_factory() as session:
        await _load_run(session, run_id, tenant_id)
        rows = list(await session.scalars(select(AuditTrace).where(AuditTrace.run_id == run_id)))
    rows.sort(key=lambda row: row.ts.isoformat() if row.ts else "")
    audit_rows = [
        AuditRow(
            **redact_json(
                {
                    "id": row.id,
                    "ts": row.ts,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "action": row.action,
                    "actor": row.actor,
                    "decision": row.decision,
                    "reason": row.reason,
                    "evidence": row.evidence,
                }
            )
        )
        for row in rows
    ]
    return AuditExport(run_id=run_id, count=len(audit_rows), rows=audit_rows)
