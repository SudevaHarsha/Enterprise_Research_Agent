"""Stage 5 — extract: draft statements from each normalized source's passages."""

from __future__ import annotations

from prefect import task
from sqlalchemy import select

from app.db.models import Passage, Source, Statement
from app.pipeline.context import PipelineContext, StageResult


@task(name="pipeline.extract")
async def run_extract(ctx: PipelineContext) -> StageResult:
    """Extract draft statements from every passage of the run's sources.

    Passages have no run_id column, so the source ids are resolved first
    (single query each — no N+1). Statements are skipped when an identical
    (passage_id, text) already exists so resume never duplicates extractions.
    """
    services = ctx.services
    async with ctx.session_factory() as session:
        sources = await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
        source_ids = [source.id for source in sources if source.status == "normalized"]
        if not source_ids:
            return StageResult(stage="extract", ok=True, detail={"statements": 0})
        passages = await session.scalars(select(Passage).where(Passage.source_id.in_(source_ids)))
        existing = {
            (statement.passage_id, statement.text)
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
        }
    count = 0
    for passage in passages:
        if (passage.id, passage.text) in existing:
            continue
        statements = await services.extractor.extract(passage, ctx.run_id)
        count += len(statements)
    return StageResult(stage="extract", ok=True, detail={"statements": count})
