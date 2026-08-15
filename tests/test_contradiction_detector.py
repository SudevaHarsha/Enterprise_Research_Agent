"""Unit tests for the flag-first/confirm-second contradiction detector (task_008).

Hermetic: the gateway is wired to the fake provider/cache/meter stack plus a
local buffered fake session that can hold Contradiction and AuditTrace rows,
evaluate compound ``and_(...)`` idempotence lookups, and simulate commit
failures for atomicity assertions. Covers: deterministic candidate pruning
before any LLM (G-03), flag-first gate, confirm-second (deterministic negation
signal OR second judge opinion), only-confirmed persistence, only verified
statements processed, G-01 prompt separation, G-05 redaction, G-11 quarantine,
strong-tier routing, atomic contradiction + audit rows, idempotent skip,
the Step 14 recall hook (detect returns confirmed rows), and the span +
metrics log line.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.db.enums import ContradictionStatus, StatementStatus
from app.db.models import AuditTrace, Contradiction, KVEntry, Statement
from app.services.contradiction_detector import (
    CANDIDATE_OVERLAP_THRESHOLD,
    CONFIRM_OVERLAP_THRESHOLD,
    NEGATION_MARKERS,
    ContradictionDetector,
    build_confirm_prompt,
    build_flag_prompt,
    candidate_pairs,
    containment_overlap,
    has_negation_markers,
    jaccard_overlap,
    negation_signal,
    tokenize,
)
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway, QuarantineError
from tests.conftest import (
    FakeProvider,
    FakeResponse,
    FakeSession,
    FakeSessionFactory,
    make_run_row,
    rows_of,
)

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted

AFFIRMATIVE = "The vaccine is safe for all age groups."
NEGATED = "The vaccine is not safe for all age groups."
PROFITS = "Company X reported record profits this quarter."
OPPOSING = "Company X reported heavy losses this quarter."
UNRELATED = "Berlin weather was sunny all week."
RETAIL = "Retailers reported stronger same-store sales growth."
NEG_MARKER_LOW_OVERLAP_A = "Acme announced record profits this quarter."
NEG_MARKER_LOW_OVERLAP_B = "The report disputes the Acme profit announcement."
CONSISTENT_A = "Company Y plans to open ten new stores next year."
CONSISTENT_B = "Company Y is opening ten new stores next year."

FLAG_VERDICT = json.dumps(
    {
        "contradictory": True,
        "flag": "flag",
        "reason": "The two statements assert logically incompatible facts.",
        "confidence": 0.92,
    }
)
NO_FLAG_VERDICT = json.dumps(
    {
        "contradictory": False,
        "flag": "no_flag",
        "reason": "The two statements describe the same plan without conflict.",
        "confidence": 0.85,
    }
)
CONFIRM_TRUE = json.dumps(
    {
        "contradictory": True,
        "reason": "Profits and losses in the same quarter cannot both be true.",
        "confidence": 0.9,
    }
)
CONFIRM_FALSE = json.dumps(
    {
        "contradictory": False,
        "reason": "The statements are compatible when read in context.",
        "confidence": 0.7,
    }
)


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


class ContradictionFakeSession(FakeSession):
    """FakeSession with buffered writes, compound-and lookups, commit failures.

    ``add`` buffers Contradiction/AuditTrace rows; ``commit`` applies the
    buffer (or raises when ``fail_on_commit``); ``rollback`` discards it.
    ``scalar`` additionally understands compound ``and_(...)`` WHERE clauses
    used by the idempotence lookup.
    """

    def __init__(self, storage: dict[Any, Any]) -> None:
        super().__init__(storage)
        self._pending: list[Any] = []
        self.fail_on_commit = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, (Contradiction, AuditTrace)):
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

    async def scalar(self, statement: Any) -> Any | None:
        descriptions = getattr(statement, "column_descriptions", None)
        whereclause = getattr(statement, "whereclause", None)
        if not descriptions or whereclause is None:
            raise NotImplementedError(
                "ContradictionFakeSession.scalar: select requires a where clause"
            )
        entity = descriptions[0].get("entity")
        if entity is None:
            raise NotImplementedError("ContradictionFakeSession.scalar: unsupported select entity")
        clauses = getattr(whereclause, "clauses", None)
        if clauses is None:
            return await super().scalar(statement)
        conditions: list[tuple[str, Any]] = []
        for clause in clauses:
            column_key = getattr(getattr(clause, "left", None), "key", None)
            if column_key is None:
                raise NotImplementedError(
                    "ContradictionFakeSession.scalar: unsupported compound WHERE clause"
                )
            conditions.append((column_key, getattr(clause.right, "value", clause.right)))
        for obj in self._storage.values():
            if isinstance(obj, entity) and all(
                getattr(obj, key) == expected for key, expected in conditions
            ):
                return obj
        return None


class ContradictionSessionFactory(FakeSessionFactory):
    """Session factory that records sessions and can fail the next commit."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.sessions: list[ContradictionFakeSession] = []
        self.fail_next_commit = False

    def __call__(self) -> ContradictionFakeSession:
        session = ContradictionFakeSession(self.storage)
        if self.fail_next_commit:
            session.fail_on_commit = True
            self.fail_next_commit = False
        self.sessions.append(session)
        return session


class Harness:
    """Wiring for one hermetic contradiction test: fake stack + seeded run."""

    def __init__(self) -> None:
        self.settings = Settings(
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
        )
        self.factory = ContradictionSessionFactory()
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
        self.detector = ContradictionDetector(gateway=self.gateway, session_factory=self.factory)
        self.run = make_run_row(cost_spent_usd=Decimal("0.0000"))
        self.factory.storage[self.run.id] = self.run
        self.tracer = FakeTracer()

    def statement(
        self,
        text: str = AFFIRMATIVE,
        status: str = StatementStatus.VERIFIED.value,
        id_int: int | None = None,
    ) -> Statement:
        """Create a Statement (verified by default) and mirror it into storage.

        ``id_int`` produces deterministic UUIDs so candidate-pair ordering is
        stable across runs.
        """
        statement = Statement(
            id=UUID(int=id_int) if id_int is not None else uuid4(),
            passage_id=uuid4(),
            run_id=self.run.id,
            text=text,
            status=status,
        )
        self.factory.storage[statement.id] = statement
        return statement

    def confirmed_row(self, a: Statement, b: Statement) -> Contradiction:
        """Pre-seed an already-confirmed contradiction pair (idempotence)."""
        row = Contradiction(
            id=uuid4(),
            run_id=self.run.id,
            statement_a_id=a.id,
            statement_b_id=b.id,
            status=ContradictionStatus.CONFIRMED.value,
            evidence={"method": "confirm:negation_signal"},
            confirmed_at=datetime.now(UTC),
        )
        self.factory.storage[row.id] = row
        return row


@pytest.fixture
def harness() -> Harness:
    """Fresh hermetic wiring per test."""
    return Harness()


def test_candidate_pairs_prunes_unrelated_and_is_deterministic(harness: Harness) -> None:
    """Unrelated pairs never reach the LLM; generation is deterministic (G-03)."""
    consistent_a = harness.statement(CONSISTENT_A, id_int=1)
    consistent_b = harness.statement(CONSISTENT_B, id_int=2)
    unrelated = harness.statement(UNRELATED, id_int=3)
    pruned = harness.statement(RETAIL, id_int=4)
    statements = [consistent_a, consistent_b, unrelated, pruned]

    pairs = candidate_pairs(statements)

    assert pairs == [(consistent_a, consistent_b)]
    assert candidate_pairs(statements) == [(consistent_a, consistent_b)]  # deterministic
    assert harness.provider.calls == []  # no LLM touched by generation


def test_candidate_pairs_keeps_negation_marker_pairs(harness: Harness) -> None:
    """Negation markers rescue low-overlap pairs from pruning."""
    a = harness.statement(NEG_MARKER_LOW_OVERLAP_A, id_int=1)
    b = harness.statement(NEG_MARKER_LOW_OVERLAP_B, id_int=2)

    pairs = candidate_pairs([a, b])

    assert pairs == [(a, b)]
    assert jaccard_overlap(tokenize(a.text), tokenize(b.text)) < CANDIDATE_OVERLAP_THRESHOLD
    assert has_negation_markers(b.text) is True


def test_candidate_pairs_only_verified(harness: Harness) -> None:
    """Only status='verified' statements are considered (defensive filter)."""
    draft = harness.statement(AFFIRMATIVE, status=StatementStatus.DRAFT.value, id_int=1)
    quarantined = harness.statement(NEGATED, status=StatementStatus.QUARANTINED.value, id_int=2)
    verified = harness.statement(PROFITS, id_int=3)

    assert candidate_pairs([draft, quarantined, verified]) == []


def test_negation_heuristic_constants_and_signal() -> None:
    """Documented constants and the affirmative-vs-negated signal contract."""
    assert pytest.approx(0.15) == CANDIDATE_OVERLAP_THRESHOLD
    assert pytest.approx(0.4) == CONFIRM_OVERLAP_THRESHOLD
    assert "not" in NEGATION_MARKERS
    assert negation_signal(AFFIRMATIVE, NEGATED) is True
    assert negation_signal(NEGATED, AFFIRMATIVE) is True  # direction-agnostic
    assert negation_signal(AFFIRMATIVE, AFFIRMATIVE) is False
    assert negation_signal(PROFITS, OPPOSING) is False


def test_overlap_helpers_are_pure() -> None:
    """tokenize / jaccard / containment are deterministic pure functions."""
    a_tokens = tokenize(AFFIRMATIVE)
    b_tokens = tokenize(NEGATED)
    assert a_tokens == {"the", "vaccine", "is", "safe", "for", "all", "age", "groups"}
    assert jaccard_overlap(a_tokens, b_tokens) == pytest.approx(8 / 9)
    assert containment_overlap(a_tokens, b_tokens) == pytest.approx(8 / 9)
    assert jaccard_overlap(set(), set()) == 0.0
    assert containment_overlap(set(), b_tokens) == 0.0
    assert has_negation_markers(NEGATED) is True
    assert has_negation_markers(AFFIRMATIVE) is False


def test_prompt_builders_pure_and_deterministic() -> None:
    """Prompt builders are pure functions with delimited data blocks (G-01)."""
    system, data = build_flag_prompt(AFFIRMATIVE, NEGATED)
    assert "<statement_a_data>" in data
    assert "<statement_b_data>" in data
    assert "<statement_a_data" not in system
    assert "<statement_b_data" not in system
    assert build_flag_prompt(AFFIRMATIVE, NEGATED) == (system, data)
    confirm_system, confirm_data = build_confirm_prompt(PROFITS, OPPOSING)
    assert "<statement_a_data>" in confirm_data
    assert "<statement_b_data>" in confirm_data
    assert "<statement_a_data" not in confirm_system


async def test_flag_then_negation_signal_confirms_without_second_judge(
    harness: Harness,
) -> None:
    """Flag + deterministic negation signal -> confirmed with ONE judge call."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))

    rows = await harness.detector.detect([a, b], harness.run.id)

    assert len(harness.provider.calls) == 1
    assert harness.provider.calls[0]["model"] == "fake/strong-model"
    assert len(rows) == 1
    row = rows[0]
    assert row.statement_a_id == a.id
    assert row.statement_b_id == b.id
    assert row.status == ContradictionStatus.CONFIRMED.value
    assert row.confirmed_at is not None
    assert row.evidence["method"] == "confirm:negation_signal"
    assert row.evidence["flag_reason"] == "The two statements assert logically incompatible facts."
    assert row.evidence["flag_confidence"] == 0.92
    assert row.evidence["confirm_confidence"] is None
    assert row.evidence["negation_signal"] is True
    assert row.evidence["overlap_ratio"] == pytest.approx(8 / 9)
    assert harness.factory.sessions[0].committed is True


async def test_flag_no_flag_persists_nothing(harness: Harness) -> None:
    """Judge says no_flag -> NO row is persisted, no confirm call."""
    a = harness.statement(CONSISTENT_A, id_int=1)
    b = harness.statement(CONSISTENT_B, id_int=2)
    harness.provider.queue(FakeResponse(NO_FLAG_VERDICT))

    rows = await harness.detector.detect([a, b], harness.run.id)

    assert rows == []
    assert rows_of(harness.factory.storage, Contradiction) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert len(harness.provider.calls) == 1


async def test_flag_then_confirm_judge_confirms(harness: Harness) -> None:
    """Flag without a negation signal -> second judge opinion confirms."""
    a = harness.statement(PROFITS, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))
    harness.provider.queue(FakeResponse(CONFIRM_TRUE))

    rows = await harness.detector.detect([a, b], harness.run.id)

    assert len(harness.provider.calls) == 2
    assert len(rows) == 1
    row = rows[0]
    assert row.status == ContradictionStatus.CONFIRMED.value
    assert row.evidence["method"] == "confirm:judge"
    assert (
        row.evidence["confirm_reason"]
        == "Profits and losses in the same quarter cannot both be true."
    )
    assert row.evidence["confirm_confidence"] == 0.9
    assert row.evidence["negation_signal"] is False


async def test_flag_then_confirm_judge_rejects_without_row(harness: Harness) -> None:
    """Second judge rejects -> NO row persisted (rejected is never stored)."""
    a = harness.statement(PROFITS, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))
    harness.provider.queue(FakeResponse(CONFIRM_FALSE))

    rows = await harness.detector.detect([a, b], harness.run.id)

    assert rows == []
    assert rows_of(harness.factory.storage, Contradiction) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert len(harness.provider.calls) == 2


async def test_audit_row_shape_for_confirmed(harness: Harness) -> None:
    """Every confirmed contradiction appends one audit verdict row (atomic)."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))

    rows = await harness.detector.detect([a, b], harness.run.id)

    audit = rows_of(harness.factory.storage, AuditTrace)
    assert len(audit) == 1
    row = audit[0]
    assert row.entity_type == "contradiction"
    assert row.entity_id == str(rows[0].id)
    assert row.action == "contradiction"
    assert row.actor == "detector"
    assert row.decision == ContradictionStatus.CONFIRMED.value
    assert row.reason  # non-empty
    assert row.evidence["method"] == "confirm:negation_signal"


async def test_commit_failure_rolls_back_contradiction_and_audit(harness: Harness) -> None:
    """Atomicity: contradiction row + audit row are all-or-nothing."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))
    harness.factory.fail_next_commit = True

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await harness.detector.detect([a, b], harness.run.id)

    assert rows_of(harness.factory.storage, Contradiction) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_quarantine_error_propagates_without_rows(harness: Harness) -> None:
    """G-11: schema failure on every retry -> QuarantineError, nothing persisted."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.provider.queue(FakeResponse("not json"))
    harness.provider.queue(FakeResponse("also not json"))
    harness.provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError):
        await harness.detector.detect([a, b], harness.run.id)

    assert len(harness.provider.calls) == 3  # all retry approaches exhausted
    assert rows_of(harness.factory.storage, Contradiction) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_flag_prompt_separates_instructions_from_data(harness: Harness) -> None:
    """G-01: system holds only instructions; user holds delimited data blocks."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))

    await harness.detector.detect([a, b], harness.run.id)

    messages = harness.provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "contradict" in str(messages[0]["content"]).lower()
    assert "<statement_a_data" not in str(messages[0]["content"])
    assert "<statement_b_data" not in str(messages[0]["content"])
    assert messages[1]["role"] == "user"
    assert "<statement_a_data>" in str(messages[1]["content"])
    assert "<statement_b_data>" in str(messages[1]["content"])
    # the gateway appends its own schema instruction as a final system message
    assert messages[-1]["role"] == "system"


async def test_secret_redacted_from_prompts_and_persisted_rows(harness: Harness) -> None:
    """G-05: a secret never reaches a judge prompt or any persisted row."""
    secret_a = f"{PROFITS} Credentials: {SECRET}."
    a = harness.statement(secret_a, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    secret_flag = json.dumps(
        {
            "contradictory": True,
            "flag": "flag",
            "reason": f"The statements conflict. Credentials: {SECRET}.",
            "confidence": 0.92,
        }
    )
    secret_confirm = json.dumps(
        {
            "contradictory": True,
            "reason": f"Profits and losses conflict. Credentials: {SECRET}.",
            "confidence": 0.9,
        }
    )
    harness.provider.queue(FakeResponse(secret_flag))
    harness.provider.queue(FakeResponse(secret_confirm))

    rows = await harness.detector.detect([a, b], harness.run.id)

    # redacted before the prompts: the provider never sees the secret
    for call in harness.provider.calls:
        for message in call["messages"]:
            assert SECRET not in str(message.get("content"))
    assert "[REDACTED_API_KEY]" in str(harness.provider.calls[0]["messages"][1]["content"])
    # redacted before persist: contradiction evidence carries no secret
    assert SECRET not in str(rows[0].evidence)
    assert "[REDACTED_API_KEY]" in str(rows[0].evidence)
    # audit reason and evidence carry no secret
    audit = rows_of(harness.factory.storage, AuditTrace)[0]
    assert SECRET not in audit.reason
    assert "[REDACTED_API_KEY]" in audit.reason
    assert SECRET not in str(audit.evidence)
    # use_cache=False means the model output is never persisted anywhere
    assert rows_of(harness.factory.storage, KVEntry) == []


async def test_flag_and_confirm_use_strong_tier_only(harness: Harness) -> None:
    """Both judge passes route through tier='strong' via the gateway only."""
    a = harness.statement(PROFITS, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))
    harness.provider.queue(FakeResponse(CONFIRM_TRUE))

    await harness.detector.detect([a, b], harness.run.id)

    assert len(harness.provider.calls) == 2
    assert [call["model"] for call in harness.provider.calls] == [
        "fake/strong-model",
        "fake/strong-model",
    ]


async def test_already_confirmed_pair_is_skipped(harness: Harness) -> None:
    """Idempotence: an existing confirmed pair is skipped with no judge calls."""
    a = harness.statement(AFFIRMATIVE, id_int=1)
    b = harness.statement(NEGATED, id_int=2)
    harness.confirmed_row(a, b)

    rows = await harness.detector.detect([a, b], harness.run.id)

    assert rows == []
    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, AuditTrace) == []


async def test_empty_or_all_pruned_input_does_no_work(harness: Harness) -> None:
    """Empty or fully-pruned input sets cost zero tokens and write nothing."""
    assert await harness.detector.detect([], harness.run.id) == []

    a = harness.statement(UNRELATED, id_int=1)
    b = harness.statement(RETAIL, id_int=2)
    rows = await harness.detector.detect([a, b], harness.run.id)

    assert rows == []
    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, Contradiction) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []


async def test_detect_returns_confirmed_rows_for_recall_hook(harness: Harness) -> None:
    """Step 14 gold-set recall hook: detect returns every newly confirmed row."""
    a = harness.statement(PROFITS, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    c = harness.statement(CONSISTENT_A, id_int=3)
    d = harness.statement(CONSISTENT_B, id_int=4)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))  # (a, b) confirm judge
    harness.provider.queue(FakeResponse(CONFIRM_TRUE))
    harness.provider.queue(FakeResponse(FLAG_VERDICT))  # (c, d) confirm judge
    harness.provider.queue(FakeResponse(CONFIRM_TRUE))

    rows = await harness.detector.detect([a, b, c, d], harness.run.id)

    assert len(rows) == 2
    assert len(rows_of(harness.factory.storage, Contradiction)) == 2
    assert len(rows_of(harness.factory.storage, AuditTrace)) == 2


async def test_detect_metrics_span_and_logger(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every detect() call emits a span + cumulative metrics log line."""
    monkeypatch.setattr(
        "app.services.contradiction_detector.get_tracer", lambda name: harness.tracer
    )
    a = harness.statement(PROFITS, id_int=1)
    b = harness.statement(OPPOSING, id_int=2)
    c = harness.statement(CONSISTENT_A, id_int=3)
    d = harness.statement(CONSISTENT_B, id_int=4)
    e = harness.statement(UNRELATED, id_int=5)
    f = harness.statement(RETAIL, id_int=6)
    harness.provider.queue(FakeResponse(FLAG_VERDICT))  # (a, b) flag
    harness.provider.queue(FakeResponse(CONFIRM_FALSE))  # (a, b) rejected
    harness.provider.queue(FakeResponse(FLAG_VERDICT))  # (c, d) flag
    harness.provider.queue(FakeResponse(CONFIRM_TRUE))  # (c, d) confirmed

    with caplog.at_level(logging.INFO, logger="app.services.contradiction_detector"):
        await harness.detector.detect([a, b, c, d, e, f], harness.run.id)

    assert "contradiction_metrics" in caplog.text
    assert "confirmed=1" in caplog.text
    assert "rejected=1" in caplog.text
    assert "contradiction_rejected" in caplog.text
    assert [span.name for span in harness.tracer.spans] == ["contradiction.detect"]
    span = harness.tracer.spans[0]
    assert span.attributes["pairs_considered"] == 15  # 6 verified -> 15 raw pairs
    assert span.attributes["candidates"] == 2
    assert span.attributes["flagged"] == 2
    assert span.attributes["confirmed"] == 1
    assert span.attributes["rejected"] == 1
