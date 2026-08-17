"""Stage 8 — detect: flag/confirm contradictions among verified statements."""

from __future__ import annotations

from prefect import task
from sqlalchemy import select

from app.db.models import Statement
from app.pipeline.context import PipelineContext, StageResult

MAX_DETECT_STATEMENTS = 15  # cap verified statements sent to detector


@task(name="pipeline.detect")
async def run_detect(ctx: PipelineContext) -> StageResult:
    """Run contradiction detection over the run's verified statements (capped)."""
    services = ctx.services
    async with ctx.session_factory() as session:
        verified = [
            statement
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
            if statement.status == "verified"
        ]
    input_stmts = verified[:MAX_DETECT_STATEMENTS]
    contradictions = await services.contradiction_detector.detect(input_stmts, ctx.run_id)
    detail: dict[str, int] = {"contradictions": len(contradictions)}
    if len(verified) > MAX_DETECT_STATEMENTS:
        detail["capped_from"] = len(verified)
    return StageResult(stage="detect", ok=True, detail=detail)
