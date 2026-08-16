"""Stage 10 — trace: export the run's audit trail + report bundle, finalize."""

from __future__ import annotations

from datetime import UTC, datetime

from prefect import task
from sqlalchemy import select

from app.db.models import AuditTrace, Run
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.context import PipelineContext, StageResult
from app.services.audit_writer import redact_json

TRACE_TTL_DAYS = 30
_TRACE_TTL_SECONDS = TRACE_TTL_DAYS * 24 * 60 * 60


@task(name="pipeline.trace")
async def run_trace(ctx: PipelineContext) -> StageResult:
    """Export the immutable audit trail + report bundle to ``trace:{run_id}``.

    Deterministic and LLM-free. Every exported field is redacted (G-05); the
    kv_cache entry lives 30 days. Then the run is finalized: status
    ``completed``, progress ``1.0``, ``completed_at`` stamped.
    """
    services = ctx.services
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is None:
            raise ValueError(f"no Run row for run_id={ctx.run_id}")
        question = run.question
        audit_rows = list(
            await session.scalars(select(AuditTrace).where(AuditTrace.run_id == ctx.run_id))
        )
    conclude_cp = await CheckpointStore(ctx.session_factory).load(ctx.run_id, "conclude")
    report = (conclude_cp or {}).get("report")
    audit_trace = [
        redact_json(
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "action": row.action,
                "actor": row.actor,
                "decision": row.decision,
                "reason": row.reason,
                "evidence": row.evidence,
                "ts": row.ts.isoformat() if row.ts is not None else None,
            }
        )
        for row in audit_rows
    ]
    payload = {
        "run_id": str(ctx.run_id),
        "question": redact_json(question),
        "audit_trace": audit_trace,
        "report": report,
    }
    artifact = f"trace:{ctx.run_id}"
    await services.cache.set(
        key=artifact,
        model="pipeline/trace",
        prompt_hash="trace-stage",
        payload=payload,
        ttl_seconds=_TRACE_TTL_SECONDS,
    )
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is not None:
            run.stage = "done"
            run.status = "completed"
            run.progress = 1.0
            run.completed_at = datetime.now(UTC)
            await session.commit()
    return StageResult(stage="trace", ok=True, detail={"artifact": artifact})
