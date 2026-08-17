"""Stage 5 — extract: draft statements from each normalized source's passages."""

from __future__ import annotations

import asyncio

from prefect import task
from sqlalchemy import select

from app.db.models import Passage, Source, Statement
from app.pipeline.context import PipelineContext, StageResult

MAX_EXTRACT_PASSAGES = 20  # cap passages per run to control LLM cost
EXTRACT_DELAY_SECONDS = 5  # pacing: 15 RPM limit → ≥4s between calls


@task(name="pipeline.extract")
async def run_extract(ctx: PipelineContext) -> StageResult:
    """Extract draft statements from up to MAX_EXTRACT_PASSAGES passages.

    Passages have no run_id column, so the source ids are resolved first
    (single query each — no N+1). Statements are skipped when an identical
    (passage_id, text) already exists so resume never duplicates extractions.

    A 5-second delay between calls respects the 15 RPM Gemini quota.
    """
    services = ctx.services
    async with ctx.session_factory() as session:
        sources = await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
        source_ids = [source.id for source in sources if source.status == "normalized"]
        if not source_ids:
            return StageResult(stage="extract", ok=True, detail={"statements": 0})
        all_passages = await session.scalars(select(Passage).where(Passage.source_id.in_(source_ids)))
        existing = {
            (statement.passage_id, statement.text)
            for statement in await session.scalars(
                select(Statement).where(Statement.run_id == ctx.run_id)
            )
        }
    passages = list(all_passages)[:MAX_EXTRACT_PASSAGES]
    skipped = len(list(all_passages)) - len(passages)
    count = 0
    for idx, passage in enumerate(passages):
        if idx > 0:
            await asyncio.sleep(EXTRACT_DELAY_SECONDS)
        if (passage.id, passage.text) in existing:
            continue
        statements = await services.extractor.extract(passage, ctx.run_id)
        count += len(statements)
    detail: dict[str, int] = {"statements": count}
    if skipped:
        detail["skipped_passages"] = skipped
    return StageResult(stage="extract", ok=True, detail=detail)
