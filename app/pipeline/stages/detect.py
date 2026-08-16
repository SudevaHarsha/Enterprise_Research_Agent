"""Stage 8 — detect: flag/confirm contradictions among verified statements."""

from __future__ import annotations

from prefect import task
from sqlalchemy import select

from app.db.models import Statement
from app.pipeline.context import PipelineContext, StageResult


@task(name="pipeline.detect")
async def run_detect(ctx: PipelineContext) -> StageResult:
    """Run contradiction detection over the run's verified statements."""
    services = ctx.services
    async with ctx.session_factory() as session:
        verified = [
            statement
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
            if statement.status == "verified"
        ]
    contradictions = await services.contradiction_detector.detect(verified, ctx.run_id)
    return StageResult(
        stage="detect",
        ok=True,
        detail={"contradictions": len(contradictions)},
    )
