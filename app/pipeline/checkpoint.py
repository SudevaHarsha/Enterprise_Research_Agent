"""Durable stage checkpoints for crash-safe resume (task_011).

One ``checkpoints`` row per ``(run_id, stage)`` — a JSONB ``state`` snapshot
of the stage's JSON-serializable result. After a crash or pause, the pipeline
reads ``completed_stages`` and resumes from the first missing stage without
re-running any completed work (the resume test asserts services are never
re-invoked for checkpointed stages).

G-05: state is redacted via :func:`audit_writer.redact_json` before
persistence, so secret-looking substrings never land in the checkpoints table.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Checkpoint
from app.services.audit_writer import redact_json

SessionFactory = Callable[[], AsyncSession]


class CheckpointStore:
    """Read/write access to durable stage checkpoints."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def save(
        self,
        run_id: UUID | str,
        stage: str,
        state: dict[str, Any] | None,
    ) -> Checkpoint:
        """Upsert the checkpoint for ``(run_id, stage)``; state is redacted.

        Raises ``ValueError`` when ``state`` is not JSON-serializable — a
        checkpoint must always roundtrip through JSONB.
        """
        if state is not None:
            try:
                json.dumps(state)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"checkpoint state for stage {stage!r} must be JSON-serializable: {exc}"
                ) from exc
        redacted = redact_json(state) if state is not None else None
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(Checkpoint).where(
                    and_(Checkpoint.run_id == run_id, Checkpoint.stage == stage)
                )
            )
            if existing is None:
                row = Checkpoint(id=uuid4(), run_id=run_id, stage=stage, state=redacted)
                session.add(row)
                await session.commit()
                return row
            existing.state = redacted
            await session.commit()
            return existing

    async def load(self, run_id: UUID | str, stage: str) -> dict[str, Any] | None:
        """Return the state snapshot for ``(run_id, stage)`` or None."""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Checkpoint).where(
                    and_(Checkpoint.run_id == run_id, Checkpoint.stage == stage)
                )
            )
            if row is None:
                return None
            return dict(row.state) if row.state is not None else None

    async def completed_stages(self, run_id: UUID | str) -> set[str]:
        """Return the set of checkpointed (completed) stages for a run."""
        async with self._session_factory() as session:
            rows = await session.scalars(select(Checkpoint).where(Checkpoint.run_id == run_id))
            return {row.stage for row in rows}
