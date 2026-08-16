"""Stage 4 — store: normalize + chunk fetched sources into passages.

Loads every ``fetched`` source of the run, pulls its raw bytes from the blob
store, normalizes them (G-05 redaction applied at the text layer), chunks the
text into passages, and marks the source ``normalized``. Passage hashes use
the chunk text so already-stored chunks are skipped on resume (idempotent).
"""

from __future__ import annotations

from uuid import uuid4

from prefect import task
from sqlalchemy import select

from app.db.models import Passage, Source
from app.pipeline.context import PipelineContext, StageResult
from app.services.normalizer import content_hash


@task(name="pipeline.store")
async def run_store(ctx: PipelineContext) -> StageResult:
    """Normalize every fetched source and chunk it into Passage rows."""
    services = ctx.services
    async with ctx.session_factory() as session:
        sources = [
            source
            for source in await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
            if source.status == "fetched"
        ]
        if not sources:
            return StageResult(stage="store", ok=True, detail={"passages": 0})
        source_ids = [source.id for source in sources]
        stored_hashes = {
            passage.hash
            for passage in await session.scalars(
                select(Passage).where(Passage.source_id.in_(source_ids))
            )
        }
    total = 0
    for source in sources:
        content = await services.blob_store.get(source.raw_ref or "")
        text = services.normalizer.normalize(source.source_type, content)
        chunks = services.normalizer.chunk_passages(text)
        async with ctx.session_factory() as session:
            for seq, chunk in enumerate(chunks):
                chunk_hash = content_hash(chunk.text.encode("utf-8"))
                if chunk_hash in stored_hashes:
                    continue
                session.add(
                    Passage(
                        id=uuid4(),
                        source_id=source.id,
                        seq=seq,
                        text=chunk.text,
                        start_char=chunk.start_char,
                        end_char=chunk.end_char,
                        hash=chunk_hash,
                    )
                )
            source.status = "normalized"
            await session.commit()
        total += len(chunks)
    return StageResult(stage="store", ok=True, detail={"passages": total})
