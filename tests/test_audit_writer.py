"""Unit tests for the append-only audit_trace writer (task_007).

The writer only ever inserts — matching the append-only ``audit_trace``
governance (ORM listener + DB trigger) — and either participates in the
caller's transaction (``append``, no commit) or owns one (``record``).
G-05 redaction is applied to every string field and every string value inside
the evidence JSON before the row is handed to the session.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.db.models import AuditTrace
from app.services.audit_writer import AuditWriter
from tests.conftest import FakeSession, FakeSessionFactory, make_run_row, rows_of

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


class AuditFakeSession(FakeSession):
    """FakeSession extended to accept AuditTrace rows."""

    def add(self, obj: Any) -> None:
        if isinstance(obj, AuditTrace):
            self._storage[obj.id] = obj
        else:
            super().add(obj)

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, AuditTrace):
            self._storage.pop(obj.id, None)
        else:
            await super().delete(obj)


class AuditSessionFactory(FakeSessionFactory):
    """Session factory that records every session it hands out."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.sessions: list[AuditFakeSession] = []

    def __call__(self) -> AuditFakeSession:
        session = AuditFakeSession(self.storage)
        self.sessions.append(session)
        return session


def _writer(factory: AuditSessionFactory) -> AuditWriter:
    return AuditWriter(session_factory=factory)


async def test_append_inserts_without_committing() -> None:
    """``append`` adds to the caller's session and never commits itself."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        row = _writer(factory).append(
            session,
            run_id=run.id,
            entity_type="statement",
            entity_id=str(uuid4()),
            action="verify",
            actor="verifier",
            decision="verified",
            reason="directly supported by the passage",
            evidence={
                "support_score": "full",
                "matrix_ratio": 1.0,
                "judge_supported": True,
                "judge_confidence": 0.9,
            },
        )
    assert isinstance(row, AuditTrace)
    assert row.action == "verify"
    assert row.entity_type == "statement"
    assert row.actor == "verifier"
    assert row.decision == "verified"
    assert row.reason == "directly supported by the passage"
    assert row.evidence == {
        "support_score": "full",
        "matrix_ratio": 1.0,
        "judge_supported": True,
        "judge_confidence": 0.9,
    }
    # appended to the caller's transaction; the writer must not commit
    assert session.committed is False
    assert rows_of(factory.storage, AuditTrace) == [row]


async def test_append_requires_only_entity_type_and_action() -> None:
    """Optional fields default to None; required fields are entity_type/action."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        row = _writer(factory).append(
            session, run_id=run.id, entity_type="statement", action="verify"
        )
    assert row.entity_id is None
    assert row.actor is None
    assert row.decision is None
    assert row.reason is None
    assert row.evidence is None


async def test_record_commits_its_own_session() -> None:
    """``record`` owns a session, inserts, and commits."""
    factory = AuditSessionFactory()
    run = make_run_row()
    row = await _writer(factory).record(
        run_id=run.id,
        entity_type="statement",
        action="verify",
        decision="quarantined",
    )
    assert row.action == "verify"
    assert row.decision == "quarantined"
    assert rows_of(factory.storage, AuditTrace) == [row]
    assert len(factory.sessions) == 1
    assert factory.sessions[0].committed is True


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("entity_type", ""),
        ("entity_type", "   "),
        ("action", ""),
        ("action", "   "),
    ],
)
async def test_required_fields_reject_empty(field: str, bad: str) -> None:
    """Empty or blank required fields raise ValueError."""
    factory = AuditSessionFactory()
    run = make_run_row()
    kwargs: dict[str, Any] = {"run_id": run.id, "entity_type": "statement", "action": "verify"}
    kwargs[field] = bad
    async with factory() as session:
        with pytest.raises(ValueError, match=f"{field} is required"):
            _writer(factory).append(session, **kwargs)


@pytest.mark.parametrize("field", ["entity_type", "action", "entity_id", "actor", "decision"])
async def test_field_length_limit(field: str) -> None:
    """Any string field longer than 64 characters raises ValueError."""
    factory = AuditSessionFactory()
    run = make_run_row()
    kwargs: dict[str, Any] = {"run_id": run.id, "entity_type": "statement", "action": "verify"}
    kwargs[field] = "x" * 65
    async with factory() as session:
        with pytest.raises(ValueError, match="at most 64"):
            _writer(factory).append(session, **kwargs)


async def test_reason_length_limit() -> None:
    """A reason longer than 2000 characters raises ValueError."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        with pytest.raises(ValueError, match="reason"):
            _writer(factory).append(
                session,
                run_id=run.id,
                entity_type="statement",
                action="verify",
                reason="x" * 2001,
            )


async def test_evidence_must_be_json_object() -> None:
    """Evidence must be a JSON object (dict), not a list or scalar."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        with pytest.raises(ValueError, match="evidence must be a JSON object"):
            _writer(factory).append(
                session,
                run_id=run.id,
                entity_type="statement",
                action="verify",
                evidence=["full"],
            )


async def test_evidence_must_be_serializable() -> None:
    """Non-serializable evidence raises ValueError (never reaches the session)."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        with pytest.raises(ValueError, match="JSON-serializable"):
            _writer(factory).append(
                session,
                run_id=run.id,
                entity_type="statement",
                action="verify",
                evidence={"bad": object()},
            )


async def test_secrets_redacted_from_row_fields_and_evidence() -> None:
    """G-05: a secret in any field/evidence value is redacted before persist."""
    factory = AuditSessionFactory()
    run = make_run_row()
    async with factory() as session:
        row = _writer(factory).append(
            session,
            run_id=run.id,
            entity_type="statement",
            action="verify",
            actor="verifier",
            decision="verified",
            reason=f"The passage supports the claim. Credentials: {SECRET}.",
            evidence={"credential": SECRET, "nested": {"token": SECRET}, "ratio": 0.5},
        )
    assert SECRET not in str(row)
    assert "[REDACTED_API_KEY]" in row.reason
    assert row.evidence["credential"] == "[REDACTED_API_KEY]"
    assert row.evidence["nested"]["token"] == "[REDACTED_API_KEY]"  # noqa: S105 - dict key, not a credential
    assert row.evidence["ratio"] == 0.5
    stored = rows_of(factory.storage, AuditTrace)[0]
    assert SECRET not in str(stored)


async def test_entity_id_uuid_is_stringified() -> None:
    """A UUID entity_id is persisted as its canonical string form."""
    factory = AuditSessionFactory()
    run = make_run_row()
    entity_id = uuid4()
    async with factory() as session:
        row = _writer(factory).append(
            session,
            run_id=run.id,
            entity_type="statement",
            entity_id=entity_id,
            action="verify",
        )
    assert row.entity_id == str(entity_id)
    assert len(row.entity_id) <= 64


async def test_run_id_is_required() -> None:
    """Every audit row must belong to a run."""
    factory = AuditSessionFactory()
    async with factory() as session:
        with pytest.raises(ValueError, match="run_id is required"):
            _writer(factory).append(session, entity_type="statement", action="verify")
