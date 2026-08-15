"""Direct-URL connector — the safe ingestion pipeline (task_005).

Per-URI flow: allowlist gate -> rate-limited fetch -> G-05 redaction ->
content-hash -> dedupe -> blob persist -> G-04 unsafe filter -> normalize ->
chunk -> ``sources`` + ``passages`` rows.

Deterministic and LLM-free: dedupe and chunking cost $0 (design doc §Stages
2-3). Re-collection is idempotent: a content hash already present returns the
existing ``Source`` row without duplicating rows or blobs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from app.core.logging import get_logger
from app.db.enums import SourceStatus
from app.db.models import Passage, Source
from app.services.blob_store import BlobStore
from app.services.fetcher import Fetcher
from app.services.normalizer import (
    Normalizer,
    classify_source,
    contains_unsafe_content,
    content_hash,
    redact_bytes_for_storage,
)

logger = get_logger(__name__)


def _log_safe_uri(uri: str) -> str:
    """Strip query/fragment for logs so tokens never appear in URL strings."""
    return uri.split("?", 1)[0].split("#", 1)[0]


class URLConnector:
    """Collect a single source URI into ``sources``/``passages`` rows."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        fetcher: Fetcher,
        normalizer: Normalizer | None = None,
        blob_store: BlobStore,
    ) -> None:
        self._session_factory = session_factory
        self._fetcher = fetcher
        self._normalizer = normalizer or Normalizer()
        self._blob_store = blob_store

    async def collect(
        self,
        uri: str,
        *,
        run_id: UUID,
        title: str | None = None,
        source_type: str | None = None,
    ) -> Source | None:
        """Ingest ``uri`` into the provenance store; return the persisted Source.

        Returns the existing row when the content hash was already collected
        (idempotent re-collection), a quarantined row when G-04 flags the
        content, or the normalized row with passages otherwise.
        """
        fetched = await self._fetcher.fetch(uri, connector="url")

        safe_bytes = redact_bytes_for_storage(fetched.content)  # G-05 before persist
        digest = content_hash(safe_bytes)

        async with self._session_factory() as session:
            existing = await session.scalar(select(Source).where(Source.content_hash == digest))
            if isinstance(existing, Source):
                logger.info(
                    "collect dedupe hit (idempotent re-collection)",
                    extra={"uri": _log_safe_uri(fetched.uri), "content_hash": digest[:16]},
                )
                return existing

            effective_type = source_type or classify_source(fetched.content_type, fetched.uri).value
            text = self._normalizer.normalize(effective_type, safe_bytes)

            if contains_unsafe_content(text):  # G-04 — never amplify
                quarantined = Source(
                    id=uuid4(),
                    run_id=run_id,
                    uri=fetched.uri,
                    title=title,
                    source_type=effective_type,
                    fetched_at=fetched.fetched_at,
                    content_hash=digest,
                    allowlisted_uri=True,
                    status=SourceStatus.QUARANTINED.value,
                )
                session.add(quarantined)
                await session.commit()
                logger.warning(
                    "source quarantined (G-04)",
                    extra={"content_hash": digest[:16], "uri": _log_safe_uri(fetched.uri)},
                )
                return quarantined

            await self._blob_store.put(digest, safe_bytes)

            source = Source(
                id=uuid4(),
                run_id=run_id,
                uri=fetched.uri,
                title=title,
                source_type=effective_type,
                fetched_at=fetched.fetched_at,
                content_hash=digest,
                raw_ref=digest,
                allowlisted_uri=True,
                status=SourceStatus.NORMALIZED.value,
            )
            session.add(source)
            chunks = self._normalizer.chunk_passages(text)
            for seq, chunk in enumerate(chunks):
                session.add(
                    Passage(
                        id=uuid4(),
                        source_id=source.id,
                        seq=seq,
                        text=chunk.text,
                        start_char=chunk.start_char,
                        end_char=chunk.end_char,
                        hash=content_hash(chunk.text.encode("utf-8")),
                    )
                )
            await session.commit()
            logger.info(
                "collected source",
                extra={
                    "uri": _log_safe_uri(fetched.uri),
                    "content_hash": digest[:16],
                    "passages": len(chunks),
                    "source_type": effective_type,
                },
            )
            return source
