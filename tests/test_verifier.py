"""Unit tests for the verify-first gate (task_007).

Hermetic: the gateway is wired to the fake provider/cache/meter stack plus a
local buffered fake session that can hold Statement, EvidenceLink, and
AuditTrace rows and simulate commit failures for atomicity assertions.
Covers: verified/quarantined promotion, append-only evidence (new
method='verify' link, extractor link untouched), audit verdict rows, atomic
all-or-nothing writes, QuarantineError propagation (G-11), G-01 prompt
separation, G-05 redaction, strong-tier routing, idempotent skip unless
force, and the support-ratio metric (span + logger line).
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.enums import EvidenceScore, StatementStatus
from app.db.models import AuditTrace, EvidenceLink, KVEntry, Passage, Statement
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway, QuarantineError
from app.services.verifier import Verifier, build_judge_prompt
from tests.conftest import (
    FakeProvider,
    FakeResponse,
    FakeSession,
    FakeSessionFactory,
    make_run_row,
    rows_of,
)

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted

STATEMENT_SUPPORTED = "Retailers reported stronger same-store sales growth."
PASSAGE_SUPPORTED = "Retailers reported stronger same-store sales growth in the latest quarter."
STATEMENT_UNRELATED = "Berlin weather was sunny all week."

SUPPORTED_VERDICT = json.dumps(
    {
        "supported": True,
        "verdict": "supported",
        "reason": "The passage directly supports this claim.",
        "confidence": 0.9,
    }
)
UNSUPPORTED_VERDICT = json.dumps(
    {
        "supported": False,
        "verdict": "unsupported",
        "reason": "The passage does not directly support this claim.",
        "confidence": 0.3,
    }
)


def verify_links(storage: dict[Any, Any]) -> list[EvidenceLink]:
    """All EvidenceLink rows created by the verifier (method='verify')."""
    return [link for link in rows_of(storage, EvidenceLink) if link.method == "verify"]


class FakeSpan:
    """Minimal OTel span stand-in recording name + attributes."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, Any] = {}
        self.ended = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        self.ended = True

    def __enter__(self) -> FakeSpan:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.end()


class FakeTracer:
    """Tracer stand-in recording every span started."""

    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class VerifierFakeSession(FakeSession):
    """FakeSession with buffered writes so atomicity is observable.

    ``add`` buffers Statement/EvidenceLink/AuditTrace rows; ``commit`` applies
    the buffer (or raises when ``fail_on_commit``); ``rollback`` discards it.
    """

    def __init__(self, storage: dict[Any, Any]) -> None:
        super().__init__(storage)
        self._pending: list[Any] = []
        self.fail_on_commit = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, (Statement, EvidenceLink, AuditTrace)):
            self._pending.append(obj)
        else:
            super().add(obj)

    async def commit(self) -> None:
        if self.fail_on_commit:
            raise RuntimeError("simulated commit failure")
        for obj in self._pending:
            self._storage[obj.id] = obj
        self._pending = []
        self.committed = True

    async def rollback(self) -> None:
        self._pending = []
        self.rolled_back = True


class VerifierSessionFactory(FakeSessionFactory):
    """Session factory that records sessions and can fail the next commit."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.sessions: list[VerifierFakeSession] = []
        self.fail_next_commit = False

    def __call__(self) -> VerifierFakeSession:
        session = VerifierFakeSession(self.storage)
        if self.fail_next_commit:
            session.fail_on_commit = True
            self.fail_next_commit = False
        self.sessions.append(session)
        return session


class Harness:
    """Wiring for one hermetic verification test: fake stack + seeded run."""

    def __init__(self) -> None:
        self.settings = Settings(
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
        )
        self.factory = VerifierSessionFactory()
        self.provider = FakeProvider()
        self.cache = KVCache(session_factory=self.factory)
        self.meter = CostMeter(
            session_factory=self.factory,
            cost_fn=lambda response, model: Decimal("0.0010"),
        )
        self.gateway = LLMGateway(
            settings=self.settings,
            provider=self.provider,
            cache=self.cache,
            meter=self.meter,
        )
        self.verifier = Verifier(gateway=self.gateway, session_factory=self.factory)
        self.run = make_run_row(cost_spent_usd=Decimal("0.0000"))
        self.factory.storage[self.run.id] = self.run
        self.tracer = FakeTracer()

    def passage(self, text: str = PASSAGE_SUPPORTED) -> Passage:
        """Create a Passage row and mirror it into the fake storage."""
        passage = Passage(
            id=uuid4(),
            source_id=uuid4(),
            seq=1,
            text=text,
            start_char=0,
            end_char=len(text),
            hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        self.factory.storage[passage.id] = passage
        return passage

    def statement(
        self,
        passage: Passage,
        text: str = STATEMENT_SUPPORTED,
        status: str = StatementStatus.DRAFT.value,
    ) -> Statement:
        """Create a draft Statement bound to ``passage`` and store it."""
        statement = Statement(
            id=uuid4(),
            passage_id=passage.id,
            run_id=self.run.id,
            text=text,
            status=status,
        )
        self.factory.storage[statement.id] = statement
        return statement

    def extract_link(self, statement: Statement, passage: Passage) -> EvidenceLink:
        """Seed the extractor's method='extract' provenance link (score='none')."""
        link = EvidenceLink(
            id=uuid4(),
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=self.run.id,
            score=EvidenceScore.NONE.value,
            method="extract",
        )
        self.factory.storage[link.id] = link
        return link


@pytest.fixture
def harness() -> Harness:
    """Fresh hermetic wiring per test."""
    return Harness()


async def test_supported_statement_becomes_verified(harness: Harness) -> None:
    """Matrix full + judge supported -> status verified with a verify link."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.extract_link(statement, passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    outcome = await harness.verifier.verify(statement, passage, harness.run.id)

    assert outcome.decision is StatementStatus.VERIFIED
    assert outcome.support_score is EvidenceScore.FULL
    assert outcome.matrix_ratio == pytest.approx(1.0)
    assert outcome.judge_supported is True
    assert outcome.judge_confidence == 0.9
    assert outcome.skipped is False
    assert statement.status == StatementStatus.VERIFIED.value
    links = verify_links(harness.factory.storage)
    assert len(links) == 1
    assert links[0].statement_id == statement.id
    assert links[0].passage_id == passage.id
    assert links[0].score == EvidenceScore.FULL.value
    assert harness.factory.sessions[0].committed is True


async def test_extract_link_never_updated(harness: Harness) -> None:
    """Append-only: the extractor's link stays untouched; a NEW link is inserted."""
    passage = harness.passage()
    statement = harness.statement(passage)
    extract_link = harness.extract_link(statement, passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    await harness.verifier.verify(statement, passage, harness.run.id)

    extract_links = [
        link for link in rows_of(harness.factory.storage, EvidenceLink) if link.method == "extract"
    ]
    assert extract_links == [extract_link]
    assert extract_link.score == EvidenceScore.NONE.value  # never upgraded in place
    links = verify_links(harness.factory.storage)
    assert len(links) == 1
    assert links[0].score == EvidenceScore.FULL.value


async def test_unsupported_judge_quarantines(harness: Harness) -> None:
    """Matrix full but judge says unsupported -> status quarantined."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.provider.queue(FakeResponse(UNSUPPORTED_VERDICT))

    outcome = await harness.verifier.verify(statement, passage, harness.run.id)

    assert outcome.decision is StatementStatus.QUARANTINED
    assert outcome.judge_supported is False
    assert outcome.judge_confidence == 0.3
    assert statement.status == StatementStatus.QUARANTINED.value
    links = verify_links(harness.factory.storage)
    assert len(links) == 1
    # the link records the MATRIX score, not the judge verdict
    assert links[0].score == EvidenceScore.FULL.value


async def test_matrix_none_quarantines_without_judge(harness: Harness) -> None:
    """Matrix none -> quarantined with zero LLM spend (no judge call)."""
    passage = harness.passage()
    statement = harness.statement(passage, text=STATEMENT_UNRELATED)

    outcome = await harness.verifier.verify(statement, passage, harness.run.id)

    assert outcome.decision is StatementStatus.QUARANTINED
    assert outcome.support_score is EvidenceScore.NONE
    assert outcome.matrix_ratio == pytest.approx(0.0)
    assert outcome.judge_supported is False
    assert outcome.judge_confidence is None
    assert harness.provider.calls == []
    assert statement.status == StatementStatus.QUARANTINED.value
    links = verify_links(harness.factory.storage)
    assert len(links) == 1
    assert links[0].score == EvidenceScore.NONE.value


async def test_audit_row_shape_for_verified(harness: Harness) -> None:
    """Every verification appends one immutable verdict row to audit_trace."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    await harness.verifier.verify(statement, passage, harness.run.id)

    audit = rows_of(harness.factory.storage, AuditTrace)
    assert len(audit) == 1
    row = audit[0]
    assert row.entity_type == "statement"
    assert row.entity_id == str(statement.id)
    assert row.action == "verify"
    assert row.actor == "verifier"
    assert row.decision == StatementStatus.VERIFIED.value
    assert row.reason  # non-empty
    assert row.evidence["support_score"] == EvidenceScore.FULL.value
    assert row.evidence["matrix_ratio"] == pytest.approx(1.0)
    assert row.evidence["judge_supported"] is True
    assert row.evidence["judge_confidence"] == 0.9


async def test_commit_failure_rolls_back_all_writes(harness: Harness) -> None:
    """Atomicity: status + new link + audit row are all-or-nothing."""
    passage = harness.passage()
    stored = harness.statement(passage)
    harness.extract_link(stored, passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))
    # the next session handed out (the verifier's) fails its commit
    harness.factory.fail_next_commit = True
    # the verifier mutates a separate object; storage holds the untouched row
    statement_arg = Statement(
        id=stored.id,
        passage_id=passage.id,
        run_id=harness.run.id,
        text=stored.text,
        status=StatementStatus.DRAFT.value,
    )

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await harness.verifier.verify(statement_arg, passage, harness.run.id)

    assert stored.status == StatementStatus.DRAFT.value  # no status write survived
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert verify_links(harness.factory.storage) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_quarantine_error_propagates_without_writes(harness: Harness) -> None:
    """G-11: schema failure on every retry -> QuarantineError, nothing persisted."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.provider.queue(FakeResponse("not json"))
    harness.provider.queue(FakeResponse("also not json"))
    harness.provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError):
        await harness.verifier.verify(statement, passage, harness.run.id)

    assert len(harness.provider.calls) == 3  # all retry approaches exhausted
    assert statement.status == StatementStatus.DRAFT.value
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert verify_links(harness.factory.storage) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_judge_prompt_separates_instructions_from_data(harness: Harness) -> None:
    """G-01: system holds only instructions; user holds delimited data blocks."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    await harness.verifier.verify(statement, passage, harness.run.id)

    messages = harness.provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "supported" in str(messages[0]["content"]).lower()
    assert "<statement_data" not in str(messages[0]["content"])
    assert "<passage_data" not in str(messages[0]["content"])
    assert messages[1]["role"] == "user"
    assert "<statement_data>" in str(messages[1]["content"])
    assert "<passage_data>" in str(messages[1]["content"])
    # the gateway appends its own schema instruction as a final system message
    assert messages[-1]["role"] == "system"


async def test_secret_redacted_from_prompt_and_persisted_rows(harness: Harness) -> None:
    """G-05: a secret never reaches the judge prompt or any persisted row."""
    secret_passage = f"{PASSAGE_SUPPORTED} Credentials: {SECRET}."
    passage = harness.passage(text=secret_passage)
    statement = harness.statement(passage)
    secret_verdict = json.dumps(
        {
            "supported": True,
            "verdict": "supported",
            "reason": f"The passage supports the claim. Credentials: {SECRET}.",
            "confidence": 0.9,
        }
    )
    harness.provider.queue(FakeResponse(secret_verdict))

    await harness.verifier.verify(statement, passage, harness.run.id)

    # redacted before the prompt: the provider never sees the secret
    for call in harness.provider.calls:
        for message in call["messages"]:
            assert SECRET not in str(message.get("content"))
    assert "[REDACTED_API_KEY]" in str(harness.provider.calls[0]["messages"][1]["content"])
    # redacted before persist: audit reason and evidence carry no secret
    audit = rows_of(harness.factory.storage, AuditTrace)[0]
    assert SECRET not in audit.reason
    assert "[REDACTED_API_KEY]" in audit.reason
    assert SECRET not in str(audit.evidence)
    # use_cache=False means the model output is never persisted anywhere
    assert rows_of(harness.factory.storage, KVEntry) == []


async def test_judge_uses_strong_tier_single_gateway_call(harness: Harness) -> None:
    """The judge routes through tier='strong' via the gateway, never directly."""
    passage = harness.passage()
    statement = harness.statement(passage)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    await harness.verifier.verify(statement, passage, harness.run.id)

    assert len(harness.provider.calls) == 1
    assert harness.provider.calls[0]["model"] == "fake/strong-model"


async def test_already_verified_statement_is_skipped(harness: Harness) -> None:
    """Idempotence: non-draft statements are skipped with no new writes."""
    passage = harness.passage()
    statement = harness.statement(passage, status=StatementStatus.VERIFIED.value)

    outcome = await harness.verifier.verify(statement, passage, harness.run.id)

    assert outcome.skipped is True
    assert outcome.decision is StatementStatus.VERIFIED
    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert rows_of(harness.factory.storage, EvidenceLink) == []
    assert statement.status == StatementStatus.VERIFIED.value


async def test_force_reverifies_non_draft(harness: Harness) -> None:
    """force=True bypasses the idempotence skip and re-runs the gate."""
    passage = harness.passage()
    statement = harness.statement(passage, status=StatementStatus.VERIFIED.value)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    outcome = await harness.verifier.verify(statement, passage, harness.run.id, force=True)

    assert outcome.skipped is False
    assert outcome.decision is StatementStatus.VERIFIED
    assert len(harness.provider.calls) == 1
    assert len(rows_of(harness.factory.storage, AuditTrace)) == 1
    assert len(verify_links(harness.factory.storage)) == 1


async def test_support_ratio_metric_span_and_logger(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every verification emits a span + a cumulative support-ratio log line."""
    monkeypatch.setattr("app.services.verifier.get_tracer", lambda name: harness.tracer)
    passage = harness.passage()
    verified_stmt = harness.statement(passage)
    quarantined_stmt = harness.statement(passage, text=STATEMENT_UNRELATED)
    harness.provider.queue(FakeResponse(SUPPORTED_VERDICT))

    with caplog.at_level(logging.INFO, logger="app.services.verifier"):
        await harness.verifier.verify(verified_stmt, passage, harness.run.id)
        await harness.verifier.verify(quarantined_stmt, passage, harness.run.id)

    assert "support_ratio verified=1 total=2 ratio=0.500" in caplog.text
    assert [span.name for span in harness.tracer.spans] == [
        "statement.verify",
        "statement.verify",
    ]
    first, second = harness.tracer.spans
    assert first.attributes["decision"] == StatementStatus.VERIFIED.value
    assert first.attributes["support_score"] == EvidenceScore.FULL.value
    assert first.attributes["matrix_ratio"] == pytest.approx(1.0)
    assert second.attributes["decision"] == StatementStatus.QUARANTINED.value
    assert second.attributes["support_score"] == EvidenceScore.NONE.value
    assert second.attributes["matrix_ratio"] == pytest.approx(0.0)
    # the public metric method reports the same counters
    assert harness.verifier.log_support_ratio() == (1, 2)


def test_build_judge_prompt_pure_and_deterministic() -> None:
    """The prompt builder is a pure function with delimited data blocks."""
    system, data = build_judge_prompt(STATEMENT_SUPPORTED, PASSAGE_SUPPORTED)
    assert "<statement_data>" in data
    assert "<passage_data>" in data
    assert "<statement_data" not in system
    assert "<passage_data" not in system
    assert build_judge_prompt(STATEMENT_SUPPORTED, PASSAGE_SUPPORTED) == (system, data)
