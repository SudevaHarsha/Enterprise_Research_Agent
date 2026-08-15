"""Append-only audit_trace writer (task_007, build-plan Step 7).

Every KB write decision gets an immutable verdict row in ``audit_trace``
(design doc §7.2). This writer is the single insertion path:

- ``append(session, ...)`` participates in the **caller's** transaction —
  inserts the row on the given session and never commits, so the audit row is
  committed atomically with the write it records.
- ``record(...)`` is the standalone convenience: it owns a session, appends,
  and commits.

The writer only ever inserts — there are no UPDATE/DELETE paths anywhere,
matching the append-only governance enforced by the ORM listener and the
Postgres trigger. G-05 redaction is applied to every string field and every
string value inside the evidence JSON before the row is handed to the session.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditTrace
from app.db.session import async_session_factory
from app.services.normalizer import redact_secrets

SessionFactory = Callable[[], AsyncSession]

_MAX_FIELD_LEN = 64
_MAX_REASON_LEN = 2000


def _require_string(value: Any, field: str) -> str:
    """Validate a required string field: non-empty, bounded length."""
    text = str(value)
    if not text.strip():
        raise ValueError(f"{field} is required")
    if len(text) > _MAX_FIELD_LEN:
        raise ValueError(f"{field} must be at most {_MAX_FIELD_LEN} characters")
    return text


def _optional_string(value: Any, field: str) -> str | None:
    """Validate an optional string field: when present, non-empty and bounded."""
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        raise ValueError(f"{field} must be non-empty when provided")
    if len(text) > _MAX_FIELD_LEN:
        raise ValueError(f"{field} must be at most {_MAX_FIELD_LEN} characters")
    return text


def redact_json(value: Any) -> Any:
    """Recursively redact secret-looking substrings in a JSON-serializable value.

    Scalars that are not strings pass through untouched; dict values, list
    items, and tuple items are visited recursively (G-05).
    """
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_json(item) for item in value]
    return value


class AuditWriter:
    """Insert-only audit_trace writer participating in caller or own transaction."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory or async_session_factory

    def append(
        self,
        session: AsyncSession,
        *,
        run_id: UUID | str | None = None,
        entity_type: str,
        entity_id: UUID | str | None = None,
        action: str,
        actor: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> AuditTrace:
        """Insert one audit row on ``session`` WITHOUT committing.

        The caller owns the transaction: the audit row is committed together
        with the write it records, and rolled back together with it.
        """
        if run_id is None:
            raise ValueError("run_id is required")
        entity_type = _require_string(entity_type, "entity_type")
        action = _require_string(action, "action")
        entity_id = _optional_string(entity_id, "entity_id")
        actor = _optional_string(actor, "actor")
        decision = _optional_string(decision, "decision")
        if reason is not None:
            reason = str(reason)
            if len(reason) > _MAX_REASON_LEN:
                raise ValueError(f"reason must be at most {_MAX_REASON_LEN} characters")
        if evidence is not None:
            if not isinstance(evidence, dict):
                raise ValueError("evidence must be a JSON object")
            try:
                json.dumps(evidence)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"evidence must be JSON-serializable: {exc}") from exc
        row = AuditTrace(
            id=uuid4(),
            run_id=run_id,
            entity_type=redact_secrets(entity_type),
            entity_id=redact_secrets(entity_id) if entity_id is not None else None,
            action=redact_secrets(action),
            actor=redact_secrets(actor) if actor is not None else None,
            decision=redact_secrets(decision) if decision is not None else None,
            reason=redact_secrets(reason) if reason is not None else None,
            evidence=redact_json(evidence) if evidence is not None else None,
        )
        session.add(row)
        return row

    async def record(
        self,
        *,
        run_id: UUID | str,
        entity_type: str,
        entity_id: UUID | str | None = None,
        action: str,
        actor: str | None = None,
        decision: str | None = None,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> AuditTrace:
        """Standalone convenience: own session, insert, and commit."""
        async with self._session_factory() as session:
            row = self.append(
                session,
                run_id=run_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                actor=actor,
                decision=decision,
                reason=reason,
                evidence=evidence,
            )
            await session.commit()
            return row
