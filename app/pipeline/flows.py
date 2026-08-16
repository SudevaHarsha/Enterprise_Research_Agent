"""Research pipeline flows (task_011) — sequential 10-stage DAG with resume.

Both flows run IN-PROCESS against an async session factory + service bundle
(no Prefect server, no Docker). ``research_pipeline`` executes every stage
that lacks a durable Checkpoint row; ``resume_pipeline`` re-enters it, so a
crashed or budget-paused run picks up exactly where it stopped.

Observability: before each stage the ``runs`` row is updated (stage, status,
progress); after each stage a checkpoint is saved and the ``run.checkpoint``
mirror is refreshed. A cost breach pauses the run and emits the
``circuit_breaker_open`` alert; any other failure marks the run ``failed``
and re-raises.

task_013 (build-plan Step 13): the flow ALSO emits structured JSONL lifecycle
events (``app.pipeline.events``) and records Prometheus metrics
(``app.core.metrics``) — stage_started/stage_completed around each stage,
run_completed/paused/failed at each terminal state, stage durations into a
histogram, and the run's metered cost into a per-run gauge. All wiring is
additive: stage execution, checkpoints, budgets, and status transitions are
unchanged.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any
from uuid import UUID

from prefect import flow

from app.core import metrics
from app.core.logging import get_logger
from app.core.telemetry import get_tracer
from app.db.models import Run
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.circuit_breaker import CircuitBreaker, CircuitBreakerError
from app.pipeline.context import (
    STAGE_PROGRESS,
    STAGE_STATUS,
    STAGES,
    PipelineContext,
    PipelineServices,
)
from app.pipeline.events import (
    emit_run_completed,
    emit_run_failed,
    emit_run_paused,
    emit_stage_completed,
    emit_stage_started,
)
from app.pipeline.stages import (
    run_collect,
    run_conclude,
    run_define,
    run_detect,
    run_extract,
    run_find,
    run_search,
    run_store,
    run_trace,
    run_verify,
)

logger = get_logger("app.pipeline.flows")
_tracer = get_tracer("ecrke")

_STAGE_TASKS: dict[str, Any] = {
    "define": run_define,
    "search": run_search,
    "collect": run_collect,
    "store": run_store,
    "extract": run_extract,
    "verify": run_verify,
    "find": run_find,
    "detect": run_detect,
    "conclude": run_conclude,
    "trace": run_trace,
}


async def _mark_stage_start(ctx: PipelineContext, stage: str) -> None:
    """Publish the observable stage lifecycle (stage, status, progress)."""
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is None:
            raise ValueError(f"no Run row for run_id={ctx.run_id}")
        run.stage = stage
        run.status = STAGE_STATUS[stage]
        run.progress = STAGE_PROGRESS[stage]
        await session.commit()


async def _mark_checkpoint(ctx: PipelineContext, store: CheckpointStore) -> None:
    """Mirror the durable checkpoint set onto ``runs.checkpoint`` (JSONB)."""
    completed = await store.completed_stages(ctx.run_id)
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is not None:
            run.checkpoint = {"completed_stages": sorted(completed)}
            await session.commit()


async def _execute_stages(ctx: PipelineContext) -> str:
    """Run every stage missing a durable checkpoint; return outcome string."""
    store = CheckpointStore(ctx.session_factory)
    completed = await store.completed_stages(ctx.run_id)
    breaker = CircuitBreaker()
    async with ctx.session_factory() as session:
        run = await session.get(Run, ctx.run_id)
        if run is None:
            raise ValueError(f"no Run row for run_id={ctx.run_id}")
        run_budget = run.cost_budget_usd
    stage: str | None = None
    try:
        for stage in STAGES:
            if stage in completed:
                continue
            task = _STAGE_TASKS[stage]
            await _mark_stage_start(ctx, stage)
            emit_stage_started(ctx.run_id, stage)
            started_at = time.perf_counter()
            result = await task(ctx)
            if not result.ok:
                raise RuntimeError(f"stage {stage!r} reported failure")
            await store.save(ctx.run_id, stage, result.detail)
            await _mark_checkpoint(ctx, store)
            metrics.record_stage_duration(stage, time.perf_counter() - started_at)
            emit_stage_completed(ctx.run_id, stage, ok=True, detail=result.detail)
            if stage != STAGES[-1]:
                async with ctx.session_factory() as session:
                    run = await session.get(Run, ctx.run_id)
                    spent = run.cost_spent_usd if run is not None else None
                with _tracer.start_as_current_span("pipeline.breaker_check"):
                    breaker.check(stage, spent or Decimal("0"), run_budget or Decimal("0"))
    except CircuitBreakerError as exc:
        async with ctx.session_factory() as session:
            run = await session.get(Run, ctx.run_id)
            if run is not None:
                run.status = "paused"
                await session.commit()
        await _record_run_cost(ctx)
        logger.error("circuit_breaker_open stage=%s run_id=%s reason=%s", stage, ctx.run_id, exc)
        emit_run_paused(ctx.run_id, reason=str(exc))
        metrics.record_run_status("paused")
        return "paused"
    except Exception as exc:
        async with ctx.session_factory() as session:
            run = await session.get(Run, ctx.run_id)
            if run is not None:
                run.status = "failed"
                await session.commit()
        await _record_run_cost(ctx)
        emit_run_failed(ctx.run_id, error=str(exc))
        metrics.record_run_status("failed")
        raise
    await _record_run_cost(ctx)
    emit_run_completed(ctx.run_id)
    metrics.record_run_status("completed")
    return "completed"


async def _record_run_cost(ctx: PipelineContext) -> None:
    """Record the run's metered spend into the cost gauge (task_013).

    Observability is best-effort: a metric-recording failure must never take
    the flow down or mask the run's real status — it is logged and the flow
    continues (the run row is the durable artifact either way).
    """
    try:
        async with ctx.session_factory() as session:
            run = await session.get(Run, ctx.run_id)
            if run is not None and run.cost_spent_usd is not None:
                metrics.record_run_cost(run.id, run.cost_spent_usd)
    except Exception as exc:  # pragma: no cover - defensive, verified by tests
        logger.warning("metrics_record_cost_failed run_id=%s reason=%s", ctx.run_id, exc)


@flow(name="research-pipeline")
async def research_pipeline(run_id: UUID | str, services: PipelineServices) -> str:
    """Execute the research DAG, skipping stages with durable checkpoints."""
    return await _execute_stages(PipelineContext(run_id=run_id, services=services))


@flow(name="research-pipeline-resume")
async def resume_pipeline(run_id: UUID | str, services: PipelineServices) -> str:
    """Re-enter the pipeline; checkpointed stages are skipped automatically."""
    return await research_pipeline(run_id, services)
