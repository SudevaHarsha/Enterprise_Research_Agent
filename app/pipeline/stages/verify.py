"""Stage 6 — verify: run the verify-first gate on every draft statement."""

from __future__ import annotations

import asyncio

from prefect import task
from sqlalchemy import select

from app.db.models import Passage, Source, Statement
from app.pipeline.context import PipelineContext, StageResult


@task(name="pipeline.verify")
async def run_verify(ctx: PipelineContext) -> StageResult:
    """Verify each draft statement against its passage (verify-first gate).

    Loads all of the run's draft statements and resolves the passage map for
    the run's sources (single query each — no N+1). The verifier promotes
    drafts to ``verified`` (or ``quarantined``) and appends the evidence link.

    A 3-second delay between calls prevents bursting through provider rate
    limits (G-03: budget-aware call pacing).
    """
    services = ctx.services
    async with ctx.session_factory() as session:
        sources = await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
        source_ids = [source.id for source in sources]
        passages = {
            passage.id: passage
            for passage in (
                await session.scalars(select(Passage).where(Passage.source_id.in_(source_ids)))
                if source_ids
                else []
            )
        }
        drafts = [
            statement
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
            if statement.status == "draft"
        ]
    count = 0
    for idx, statement in enumerate(drafts):
        if idx > 0:
            await asyncio.sleep(3)
        passage = passages.get(statement.passage_id)
        if passage is None:
            continue
        await services.verifier.verify(statement, passage, ctx.run_id)
        count += 1
    return StageResult(stage="verify", ok=True, detail={"verified": count})
