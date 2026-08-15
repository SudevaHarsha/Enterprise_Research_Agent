"""Unit tests for the report generator (task_010, build-plan Step 10).

Hermetic: the gateway is wired to the fake provider/cache/meter stack plus a
local buffered fake session that can hold Conclusion, ConclusionEvidence, and
AuditTrace rows and simulate commit failures for atomicity assertions. Covers:
verified-only synthesis (draft/quarantined never reach the LLM), evidence
linkage (every conclusion cites >=1 statement), one-sidedness from source
domain diversity, contradiction warnings, high-stakes human review flags,
G-01 prompt separation, G-05 redaction, G-11 quarantine with zero writes,
strong-tier routing through the gateway (never direct provider calls), one
atomic commit for conclusion + evidence + audit rows, and ValueError on an
empty verified set before any LLM call.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.enums import ContradictionStatus, EvidenceScore, StatementStatus
from app.db.models import (
    AuditTrace,
    Conclusion,
    ConclusionEvidence,
    Contradiction,
    EvidenceLink,
    KVEntry,
    Passage,
    Source,
    Statement,
)
from app.services.cost_meter import CostMeter
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway, QuarantineError
from app.services.report_generator import (
    HIGH_STAKES_KEYWORDS,
    ReportGenerator,
    build_synthesis_prompt,
    is_high_stakes,
    one_sidedness_check,
)
from tests.conftest import (
    FakeProvider,
    FakeResponse,
    FakeSession,
    FakeSessionFactory,
    make_run_row,
    rows_of,
)

SECRET = "sk-fake-report-1234567890"  # noqa: S105 - fake fixture value; must be redacted

STMT_A = "Retailers reported stronger same-store sales growth."
STMT_B = "E-commerce expanded its share of total retail spending."
STMT_C = "Same-store sales growth slowed in the latest quarter."


def conclusion_draft(
    text: str,
    statement_ids: list[str],
    confidence: float | None = 0.8,
    one_sided: bool = False,
    high_stakes: bool = False,
) -> dict[str, Any]:
    """Build one ConclusionDraft-shaped dict for the extraction JSON fixture."""
    return {
        "text": text,
        "statement_ids": statement_ids,
        "confidence": confidence,
        "one_sided": one_sided,
        "high_stakes": high_stakes,
    }


def extraction_json(*drafts: dict[str, Any]) -> str:
    """Serialize ConclusionDraft dicts into a ConclusionExtraction-shaped response."""
    return json.dumps({"conclusions": list(drafts)})


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


class ReportFakeSession(FakeSession):
    """FakeSession with buffered writes so atomicity is observable.

    ``add`` buffers Conclusion / ConclusionEvidence / AuditTrace rows;
    ``commit`` applies the buffer (or raises when ``fail_on_commit``);
    ``rollback`` discards it. ``scalars`` evaluates single-column IN queries
    (select(Entity).where(Entity.col.in_(values))) and simple equality.
    """

    def __init__(self, storage: dict[Any, Any]) -> None:
        super().__init__(storage)
        self._pending: list[Any] = []
        self.fail_on_commit = False

    def add(self, obj: Any) -> None:
        if isinstance(obj, (Conclusion, ConclusionEvidence, AuditTrace)):
            self._pending.append(obj)
        else:
            super().add(obj)

    async def commit(self) -> None:
        if self.fail_on_commit:
            raise RuntimeError("simulated commit failure")
        for obj in self._pending:
            if isinstance(obj, ConclusionEvidence):
                self._storage[(obj.conclusion_id, obj.statement_id)] = obj
            else:
                self._storage[obj.id] = obj
        self._pending = []
        self.committed = True

    async def rollback(self) -> None:
        self._pending = []
        self.rolled_back = True

    async def scalars(self, statement: Any) -> list[Any]:
        """Minimal translator for ``select(Entity).where(Entity.col.in_(values))``.

        Also accepts ``== value``. Anything else raises ``NotImplementedError``
        so a real SQLAlchemy expression is never silently mis-evaluated.
        """
        descriptions = getattr(statement, "column_descriptions", None)
        whereclause = getattr(statement, "whereclause", None)
        if not descriptions or whereclause is None:
            raise NotImplementedError(
                "ReportFakeSession.scalars only supports "
                "select(Entity).where(Entity.col.in_(values))"
            )
        entity = descriptions[0].get("entity")
        if entity is None:
            raise NotImplementedError("ReportFakeSession.scalars: unsupported select entity")
        column = getattr(whereclause, "left", None)
        column_key = getattr(column, "key", None)
        if column_key is None:
            raise NotImplementedError("ReportFakeSession.scalars: unsupported WHERE column")
        expected = getattr(whereclause.right, "value", whereclause.right)
        values = expected if isinstance(expected, (list, tuple, set)) else [expected]
        return [
            obj
            for obj in self._storage.values()
            if isinstance(obj, entity) and getattr(obj, column_key) in values
        ]


class ReportSessionFactory(FakeSessionFactory):
    """Session factory that records sessions and can fail the next commit."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.sessions: list[ReportFakeSession] = []
        self.fail_next_commit = False

    def __call__(self) -> ReportFakeSession:
        session = ReportFakeSession(self.storage)
        if self.fail_next_commit:
            session.fail_on_commit = True
            self.fail_next_commit = False
        self.sessions.append(session)
        return session


class Harness:
    """Wiring for one hermetic report test: fake stack + seeded run."""

    def __init__(self) -> None:
        self.settings = Settings(
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
        )
        self.factory = ReportSessionFactory()
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
        self.generator = ReportGenerator(gateway=self.gateway, session_factory=self.factory)
        self.run = make_run_row(cost_spent_usd=Decimal("0.0000"))
        self.factory.storage[self.run.id] = self.run
        self.tracer = FakeTracer()

    def source(self, uri: str = "https://retail.example.com/report") -> Source:
        """Create a Source row and mirror it into the fake storage."""
        source = Source(
            id=uuid4(),
            run_id=self.run.id,
            uri=uri,
            title="Retail source",
            content_hash=hashlib.sha256(uri.encode("utf-8")).hexdigest(),
        )
        self.factory.storage[source.id] = source
        return source

    def passage(self, source: Source, text: str = STMT_A) -> Passage:
        """Create a Passage bound to ``source`` and store it."""
        passage = Passage(
            id=uuid4(),
            source_id=source.id,
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
        text: str = STMT_A,
        status: str = StatementStatus.VERIFIED.value,
    ) -> Statement:
        """Create a Statement bound to ``passage`` and store it."""
        statement = Statement(
            id=uuid4(),
            passage_id=passage.id,
            run_id=self.run.id,
            text=text,
            status=status,
        )
        self.factory.storage[statement.id] = statement
        return statement

    def verify_link(
        self,
        statement: Statement,
        passage: Passage,
        score: str = EvidenceScore.FULL.value,
    ) -> EvidenceLink:
        """Seed the verifier's method='verify' provenance link."""
        link = EvidenceLink(
            id=uuid4(),
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=self.run.id,
            score=score,
            method="verify",
            created_at=datetime.now(UTC),
        )
        self.factory.storage[link.id] = link
        return link

    def contradiction(
        self,
        a: Statement,
        b: Statement,
        reason: str = "The statements disagree on the growth direction.",
        status: str = ContradictionStatus.CONFIRMED.value,
    ) -> Contradiction:
        """Build a Contradiction row (not stored — passed directly to generate)."""
        return Contradiction(
            id=uuid4(),
            run_id=self.run.id,
            statement_a_id=a.id,
            statement_b_id=b.id,
            status=status,
            evidence={"flag_reason": reason, "confirm_reason": reason},
            confirmed_at=datetime.now(UTC),
        )


@pytest.fixture
def harness() -> Harness:
    """Fresh hermetic wiring per test."""
    return Harness()


async def test_only_verified_statements_reach_llm(harness: Harness) -> None:
    """Synthesis prompt contains only status='verified' statements."""
    source = harness.source()
    passage = harness.passage(source)
    verified = harness.statement(passage, text=STMT_A)
    draft = harness.statement(passage, text=STMT_B, status=StatementStatus.DRAFT.value)
    quarantined = harness.statement(passage, text=STMT_C, status=StatementStatus.QUARANTINED.value)
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(verified.id)])))
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[verified, draft, quarantined],
        confirmed_contradictions=[],
    )

    assert len(report.conclusions) == 1
    messages = harness.provider.calls[0]["messages"]
    user_content = str(messages[1]["content"])
    assert str(verified.id) in user_content
    assert str(draft.id) not in user_content
    assert str(quarantined.id) not in user_content
    assert STMT_B not in user_content
    assert STMT_C not in user_content


async def test_every_conclusion_has_evidence_links(harness: Harness) -> None:
    """Each persisted Conclusion cites >=1 statement; total links == cited count."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    b = harness.statement(passage, text=STMT_B)
    harness.verify_link(a, passage)
    harness.verify_link(b, passage)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft("Growth rose.", [str(a.id)]),
                conclusion_draft("E-commerce expanded.", [str(a.id), str(b.id)]),
            )
        )
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a, b],
        confirmed_contradictions=[],
    )

    conclusions = rows_of(harness.factory.storage, Conclusion)
    assert len(conclusions) == 2
    links = rows_of(harness.factory.storage, ConclusionEvidence)
    assert len(links) == 3  # 1 + 2 cited ids
    for conclusion in conclusions:
        cited = [link for link in links if link.conclusion_id == conclusion.id]
        assert len(cited) >= 1
    assert len(report.conclusions) == 2


async def test_single_source_domain_is_one_sided(harness: Harness) -> None:
    """One distinct source domain -> deterministic one_sided=True."""
    source = harness.source(uri="https://only.example.com/report")
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(a.id)])))
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    assert report.conclusions[0].one_sided is True
    assert report.conclusions[0].evidence_statements[0].source_domain == "only.example.com"


async def test_multiple_domains_not_one_sided(harness: Harness) -> None:
    """>=2 distinct source domains + LLM one_sided=False -> one_sided=False."""
    source_a = harness.source(uri="https://alpha.example.com/report")
    source_b = harness.source(uri="https://beta.example.com/report")
    passage_a = harness.passage(source_a, text=STMT_A)
    passage_b = harness.passage(source_b, text=STMT_B)
    a = harness.statement(passage_a, text=STMT_A)
    b = harness.statement(passage_b, text=STMT_B)
    harness.verify_link(a, passage_a)
    harness.verify_link(b, passage_b)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft(
                    "Growth rose across channels.",
                    [str(a.id), str(b.id)],
                    one_sided=False,
                )
            )
        )
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a, b],
        confirmed_contradictions=[],
    )

    assert report.conclusions[0].one_sided is False


async def test_contradiction_warnings_surfaced(harness: Harness) -> None:
    """Confirmed contradictions referencing cited statements become warnings."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    b = harness.statement(passage, text=STMT_C)
    harness.verify_link(a, passage)
    conflict = harness.contradiction(a, b, reason="The statements disagree on growth direction.")
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(a.id)])))
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a, b],
        confirmed_contradictions=[conflict],
    )

    assert report.conclusions[0].contradiction_warnings
    assert "disagree on growth direction" in report.conclusions[0].contradiction_warnings[0]


async def test_high_stakes_flags_human_review(harness: Harness) -> None:
    """A high-stakes keyword -> human_review_required=True; benign -> False."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft(
                    "The clinical trial dosage requires FDA approval.",
                    [str(a.id)],
                ),
                conclusion_draft("Growth rose in the latest quarter.", [str(a.id)]),
            )
        )
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    assert report.conclusions[0].human_review_required is True
    assert report.conclusions[1].human_review_required is False


async def test_synthesis_prompt_separates_instructions_from_data(harness: Harness) -> None:
    """G-01: system holds only instructions; user holds delimited data blocks."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(a.id)])))
    )

    await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    messages = harness.provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "<verified_statements" not in str(messages[0]["content"])
    assert "<confirmed_contradictions" not in str(messages[0]["content"])
    assert messages[1]["role"] == "user"
    assert "<verified_statements>" in str(messages[1]["content"])
    assert "<confirmed_contradictions>" in str(messages[1]["content"])
    # the gateway appends its own schema instruction as a final system message
    assert messages[-1]["role"] == "system"


async def test_secret_redacted_from_prompt_and_persisted_rows(harness: Harness) -> None:
    """G-05: a secret never reaches the synthesis prompt or any persisted row."""
    source = harness.source()
    passage = harness.passage(source, text=f"{STMT_A} Credentials: {SECRET}.")
    a = harness.statement(passage, text=f"{STMT_A} Credentials: {SECRET}.")
    harness.verify_link(a, passage)
    secret_draft = conclusion_draft(
        f"Growth rose. Credentials: {SECRET}.",
        [str(a.id)],
    )
    harness.provider.queue(FakeResponse(extraction_json(secret_draft)))

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic=f"Retail outlook Credentials: {SECRET}.",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    # redacted before the prompt: the provider never sees the secret
    for call in harness.provider.calls:
        for message in call["messages"]:
            assert SECRET not in str(message.get("content"))
    # redacted before persist: conclusion text, audit reason, audit evidence
    conclusions = rows_of(harness.factory.storage, Conclusion)
    assert conclusions
    for conclusion in conclusions:
        assert SECRET not in conclusion.text
        assert "[REDACTED_API_KEY]" in conclusion.text
    audit = rows_of(harness.factory.storage, AuditTrace)[0]
    assert SECRET not in audit.reason
    assert SECRET not in str(audit.evidence)
    # redacted in the returned report: topic and evidence statement text carry no secret
    assert SECRET not in report.topic
    assert "[REDACTED_API_KEY]" in report.topic
    for evidence in report.conclusions[0].evidence_statements:
        assert SECRET not in evidence.text
        assert "[REDACTED_API_KEY]" in evidence.text
    # use_cache=False means the model output is never persisted anywhere
    assert rows_of(harness.factory.storage, KVEntry) == []


async def test_quarantine_error_propagates_without_writes(harness: Harness) -> None:
    """G-11: schema failure on every retry -> QuarantineError, nothing persisted."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(FakeResponse("not json"))
    harness.provider.queue(FakeResponse("also not json"))
    harness.provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError):
        await harness.generator.generate(
            run_id=harness.run.id,
            topic="Retail outlook",
            verified_statements=[a],
            confirmed_contradictions=[],
        )

    assert len(harness.provider.calls) == 3  # all retry approaches exhausted
    assert rows_of(harness.factory.storage, Conclusion) == []
    assert rows_of(harness.factory.storage, ConclusionEvidence) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_strong_tier_single_gateway_call(harness: Harness) -> None:
    """Synthesis routes through tier='strong' via the gateway, never directly."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(a.id)])))
    )

    await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    assert len(harness.provider.calls) == 1
    assert harness.provider.calls[0]["model"] == "fake/strong-model"


async def test_audit_trace_row_per_conclusion(harness: Harness) -> None:
    """Each conclusion appends one immutable audit verdict row (action='conclude')."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft("Growth rose.", [str(a.id)]),
                conclusion_draft("Expansion continued.", [str(a.id)]),
            )
        )
    )

    await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    audit = rows_of(harness.factory.storage, AuditTrace)
    assert len(audit) == 2
    for row in audit:
        assert row.entity_type == "conclusion"
        assert row.action == "conclude"
        assert row.actor == "report_generator"
        assert row.decision == "concluded"
        assert row.reason
        assert row.evidence["statement_ids"]
        assert "one_sided" in row.evidence
        assert "high_stakes" in row.evidence


async def test_commit_failure_rolls_back_all_writes(harness: Harness) -> None:
    """Atomicity: all conclusion/evidence/audit rows are all-or-nothing."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft("Growth rose.", [str(a.id)]),
                conclusion_draft("Expansion continued.", [str(a.id)]),
            )
        )
    )
    harness.factory.fail_next_commit = True

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        await harness.generator.generate(
            run_id=harness.run.id,
            topic="Retail outlook",
            verified_statements=[a],
            confirmed_contradictions=[],
        )

    assert rows_of(harness.factory.storage, Conclusion) == []
    assert rows_of(harness.factory.storage, ConclusionEvidence) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []
    assert harness.factory.sessions[0].rolled_back is True
    assert harness.factory.sessions[0].committed is False


async def test_empty_verified_set_raises_value_error(harness: Harness) -> None:
    """No verified statements -> ValueError BEFORE any LLM call."""
    source = harness.source()
    passage = harness.passage(source)
    draft = harness.statement(passage, text=STMT_A, status=StatementStatus.DRAFT.value)

    with pytest.raises(ValueError, match="verified statement"):
        await harness.generator.generate(
            run_id=harness.run.id,
            topic="Retail outlook",
            verified_statements=[draft],
            confirmed_contradictions=[],
        )

    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, Conclusion) == []
    assert rows_of(harness.factory.storage, AuditTrace) == []


async def test_support_matrix_from_verify_links(harness: Harness) -> None:
    """Support matrix carries statement/passage ids + latest verify-link score."""
    source = harness.source()
    passage = harness.passage(source)
    a = harness.statement(passage, text=STMT_A)
    harness.verify_link(a, passage, score=EvidenceScore.FULL.value)
    harness.provider.queue(
        FakeResponse(extraction_json(conclusion_draft("Growth rose.", [str(a.id)])))
    )

    report = await harness.generator.generate(
        run_id=harness.run.id,
        topic="Retail outlook",
        verified_statements=[a],
        confirmed_contradictions=[],
    )

    assert len(report.conclusions[0].support_matrix) == 1
    entry = report.conclusions[0].support_matrix[0]
    assert entry.statement_id == str(a.id)
    assert entry.passage_id == str(passage.id)
    assert entry.support_score == EvidenceScore.FULL.value


async def test_span_and_metrics_log(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every generation emits a report.generate span + a metrics log line."""
    monkeypatch.setattr("app.services.report_generator.get_tracer", lambda name: harness.tracer)
    source_a = harness.source(uri="https://alpha.example.com/report")
    source_b = harness.source(uri="https://beta.example.com/report")
    passage_a = harness.passage(source_a, text=STMT_A)
    passage_b = harness.passage(source_b, text=STMT_B)
    a = harness.statement(passage_a, text=STMT_A)
    b = harness.statement(passage_b, text=STMT_B)
    harness.verify_link(a, passage_a)
    harness.verify_link(b, passage_b)
    harness.provider.queue(
        FakeResponse(
            extraction_json(
                conclusion_draft(
                    "Growth rose.",
                    [str(a.id)],
                    one_sided=True,
                    high_stakes=False,
                ),
                conclusion_draft(
                    "The clinical trial dosage requires FDA approval.",
                    [str(a.id), str(b.id)],
                    one_sided=False,
                    high_stakes=True,
                ),
            )
        )
    )

    with caplog.at_level(logging.INFO, logger="app.services.report_generator"):
        await harness.generator.generate(
            run_id=harness.run.id,
            topic="Retail outlook",
            verified_statements=[a, b],
            confirmed_contradictions=[],
        )

    assert "report_metrics" in caplog.text
    assert "verified_count=2" in caplog.text
    assert "conclusions=2" in caplog.text
    assert [span.name for span in harness.tracer.spans] == ["report.generate"]
    span = harness.tracer.spans[0]
    assert span.attributes["verified_count"] == 2
    assert span.attributes["conclusions"] == 2
    assert span.attributes["one_sided_count"] == 1
    assert span.attributes["human_review_count"] == 1


def test_build_synthesis_prompt_pure_and_deterministic() -> None:
    """The prompt builder is a pure function with delimited data blocks."""
    system, data = build_synthesis_prompt(
        verified_statements=[("s1", STMT_A), ("s2", STMT_B)],
        confirmed_contradictions=[("s1", "s2", "The statements disagree.")],
    )
    assert "<verified_statements>" in data
    assert "<confirmed_contradictions>" in data
    assert 'id="s1"' in data
    assert "<verified_statements" not in system
    assert "<confirmed_contradictions" not in system
    assert build_synthesis_prompt(
        verified_statements=[("s1", STMT_A), ("s2", STMT_B)],
        confirmed_contradictions=[("s1", "s2", "The statements disagree.")],
    ) == (system, data)


def test_high_stakes_keywords_documented_and_deterministic() -> None:
    """HIGH_STAKES_KEYWORDS is a non-empty frozenset; matching is casefolded."""
    assert isinstance(HIGH_STAKES_KEYWORDS, frozenset)
    assert HIGH_STAKES_KEYWORDS  # non-empty
    assert all(isinstance(word, str) for word in HIGH_STAKES_KEYWORDS)
    assert is_high_stakes("The clinical trial dosage requires FDA approval.")
    assert is_high_stakes("FDA APPROVAL IS REQUIRED")
    assert not is_high_stakes("Retail same-store sales growth continued.")


def test_one_sidedness_check_rule() -> None:
    """Rule: <2 distinct non-empty source domains OR llm_one_sided -> True."""
    assert one_sidedness_check("c", [], llm_one_sided=False) is True
    assert one_sidedness_check("c", ["only.example.com"], llm_one_sided=False) is True
    assert (
        one_sidedness_check("c", ["a.example.com", "a.example.com", ""], llm_one_sided=False)
        is True
    )
    assert (
        one_sidedness_check("c", ["a.example.com", "b.example.com"], llm_one_sided=False) is False
    )
    assert one_sidedness_check("c", ["a.example.com", "b.example.com"], llm_one_sided=True)
