"""Stage 3 — collect: allowlist-gate, fetch, safety-filter, blob-store, record.

Per candidate URI from the search checkpoint:

- denied by the allowlist (G-06 default-deny) -> ``quarantined`` source + audit
- fetch failure -> ``failed`` source + audit
- fetched but unsafe content (G-04 heuristic filter) -> ``quarantined`` source
  + audit, no blob write
- fetched -> raw bytes stored in the blob store, ``fetched`` source + audit

URIs already collected for the run are skipped (idempotent resume). ``raw_ref``
is set ONLY for fetched sources; quarantined/failed rows still get a
deterministic content hash so the NOT NULL column is satisfied.
"""

from __future__ import annotations

from uuid import uuid4

from prefect import task
from sqlalchemy import select

from app.db.models import Source
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.context import PipelineContext, StageResult
from app.services.allowlist import AllowlistDeniedError
from app.services.content_filter import is_unsafe, unsafe_reason
from app.services.fetcher import FetchError
from app.services.normalizer import classify_source, content_hash


def _decode_content(content: bytes) -> str:
    """Best-effort bytes -> text for the safety filter (never raises)."""
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


async def _record_source(
    ctx: PipelineContext,
    *,
    uri: str,
    source_type: str,
    status: str,
    allowlisted: bool,
    content_hash_value: str,
    raw_ref: str | None = None,
    action: str,
    decision: str,
    reason: str | None = None,
) -> None:
    """Insert one Source row + audit row for a collect outcome (caller commits)."""
    services = ctx.services
    async with ctx.session_factory() as session:
        source = Source(
            id=uuid4(),
            run_id=ctx.run_id,
            uri=uri,
            title=None,
            source_type=source_type,
            content_hash=content_hash_value,
            raw_ref=raw_ref,
            allowlisted_uri=allowlisted,
            status=status,
        )
        session.add(source)
        services.audit_writer.append(
            session,
            run_id=ctx.run_id,
            entity_type="source",
            entity_id=str(source.id),
            action=action,
            actor="pipeline",
            decision=decision,
            reason=reason,
            evidence={"uri": uri},
        )
        await session.commit()


@task(name="pipeline.collect")
async def run_collect(ctx: PipelineContext) -> StageResult:
    """Fetch every candidate URL from the search checkpoint into ``sources``."""
    services = ctx.services
    store = CheckpointStore(ctx.session_factory)
    search_cp = await store.load(ctx.run_id, "search")
    if search_cp is None or not search_cp.get("urls"):
        raise ValueError(
            f"search checkpoint missing URLs for run_id={ctx.run_id} (run the search stage first)"
        )
    async with ctx.session_factory() as session:
        existing = {
            source.uri
            for source in await session.scalars(select(Source).where(Source.run_id == ctx.run_id))
        }
    fetched = 0
    quarantined = 0
    failed = 0
    for uri in search_cp["urls"]:
        if uri in existing:
            continue
        # file:// URIs (mock / demo mode) bypass the G-06 egress allowlist.
        if not uri.startswith("file://"):
            try:
                services.allowlist.check(uri)
            except AllowlistDeniedError as exc:
                await _record_source(
                    ctx,
                    uri=uri,
                    source_type="other",
                    status="quarantined",
                    allowlisted=False,
                    content_hash_value=content_hash(uri.encode("utf-8")),
                    action="source.quarantined",
                    decision="quarantined",
                    reason=str(exc),
                )
                quarantined += 1
                continue
        try:
            fetched_content = await services.fetcher.fetch(uri)
        except FetchError as exc:
            await _record_source(
                ctx,
                uri=uri,
                source_type="other",
                status="failed",
                allowlisted=True,
                content_hash_value=content_hash(uri.encode("utf-8")),
                action="source.fetch_failed",
                decision="failed",
                reason=str(exc),
            )
            failed += 1
            continue
        decoded = _decode_content(fetched_content.content)
        if is_unsafe(decoded):
            await _record_source(
                ctx,
                uri=uri,
                source_type=classify_source(fetched_content.content_type, uri).value,
                status="quarantined",
                allowlisted=True,
                content_hash_value=content_hash(fetched_content.content),
                action="source.quarantined",
                decision="quarantined",
                reason=unsafe_reason(decoded),
            )
            quarantined += 1
            continue
        source_id = uuid4()
        raw_ref = f"runs/{ctx.run_id}/sources/{source_id}"
        await services.blob_store.put(raw_ref, fetched_content.content)
        await _record_source(
            ctx,
            uri=uri,
            source_type=classify_source(fetched_content.content_type, uri).value,
            status="fetched",
            allowlisted=True,
            content_hash_value=content_hash(fetched_content.content),
            raw_ref=raw_ref,
            action="source.fetched",
            decision="fetched",
            reason=None,
        )
        fetched += 1
    return StageResult(
        stage="collect",
        ok=True,
        detail={"fetched": fetched, "quarantined": quarantined, "failed": failed},
    )
