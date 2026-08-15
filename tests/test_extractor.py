"""Unit tests for the statement extractor (task_006).

Hermetic: the gateway is wired to the fake provider/cache/meter stack from
conftest plus a local ``ExtractorFakeSession`` that can hold ``Statement``
and ``EvidenceLink`` rows. Covers: draft-only extraction with passage
provenance, quarantine rollback (G-11, no partial writes), G-05 redaction of
secrets in the prompt and in persisted rows, G-01 instruction/data separation,
cheap-tier routing, empty-extraction no-op, and confidence persistence.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.enums import EvidenceScore, StatementStatus
from app.db.models import EvidenceLink, KVEntry, Passage, Run, Statement
from app.services.cost_meter import CostMeter
from app.services.extractor import Extractor
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

# Fake fixture secret matching the G-05 pattern (\bsk-[A-Za-z0-9_-]{16,}\b);
# asserted to never reach provider messages or persisted rows.
SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted

PASSAGE_TEXT = (
    "Retailers reported stronger same-store sales growth in the latest quarter. "
    "E-commerce continues to expand its share of total retail spending."
)

EXTRACTION_JSON = json.dumps(
    {
        "statements": [
            {
                "text": "Retailers reported stronger same-store sales growth.",
                "confidence": 0.9,
            },
            {
                "text": "E-commerce continues to expand its share of retail spending.",
                "confidence": 0.8,
            },
        ]
    }
)


class ExtractorFakeSession(FakeSession):
    """FakeSession extended to accept Statement and EvidenceLink rows."""

    def add(self, obj: Any) -> None:
        if isinstance(obj, (Statement, EvidenceLink)):
            self._storage[obj.id] = obj
        else:
            super().add(obj)

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, (Statement, EvidenceLink)):
            self._storage.pop(obj.id, None)
        else:
            await super().delete(obj)


class ExtractorSessionFactory(FakeSessionFactory):
    """Session factory that records every session it hands out."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.sessions: list[ExtractorFakeSession] = []

    def __call__(self) -> ExtractorFakeSession:
        session = ExtractorFakeSession(self.storage)
        self.sessions.append(session)
        return session


class Harness:
    """Wiring for one hermetic extraction test: fake stack + seeded run."""

    def __init__(self) -> None:
        self.settings = Settings(
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
        )
        self.factory = ExtractorSessionFactory()
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
        self.extractor = Extractor(gateway=self.gateway, session_factory=self.factory)
        self.run: Run = make_run_row(cost_spent_usd=Decimal("0.0000"))
        self.factory.storage[self.run.id] = self.run

    def passage(self, text: str = PASSAGE_TEXT) -> Passage:
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


@pytest.fixture
def harness() -> Harness:
    """Fresh hermetic wiring per test."""
    return Harness()


async def test_extract_writes_statements_and_evidence_links(harness: Harness) -> None:
    """Sample passage -> Statement rows plus one EvidenceLink per statement."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse(EXTRACTION_JSON))

    statements = await harness.extractor.extract(passage, harness.run.id)

    assert len(statements) == 2
    stored = rows_of(harness.factory.storage, Statement)
    assert len(stored) == 2
    assert all(s.passage_id == passage.id for s in stored)
    assert all(s.run_id == harness.run.id for s in stored)

    links = rows_of(harness.factory.storage, EvidenceLink)
    assert len(links) == 2
    for link in links:
        assert link.passage_id == passage.id
        assert link.run_id == harness.run.id
        assert link.method == "extract"
        # extraction happens before verification: score is always 'none'
        assert link.score == EvidenceScore.NONE.value
    # every link binds to a stored statement id (provenance is closed)
    stored_ids = {s.id for s in stored}
    assert {link.statement_id for link in links} == stored_ids
    # extractor persistence committed
    assert harness.factory.sessions[0].committed is True


async def test_extract_writes_draft_status_only(harness: Harness) -> None:
    """No statement leaves extraction as verified or quarantined (verify-first)."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse(EXTRACTION_JSON))

    await harness.extractor.extract(passage, harness.run.id)

    stored = rows_of(harness.factory.storage, Statement)
    assert stored
    assert all(s.status == StatementStatus.DRAFT.value for s in stored)
    assert all(
        s.status not in (StatementStatus.VERIFIED.value, StatementStatus.QUARANTINED.value)
        for s in stored
    )


async def test_quarantine_propagates_without_partial_writes(harness: Harness) -> None:
    """G-11: schema failure on every retry -> QuarantineError, no rows persisted."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse("not json"))
    harness.provider.queue(FakeResponse("also not json"))
    harness.provider.queue(FakeResponse("nope"))

    with pytest.raises(QuarantineError):
        await harness.extractor.extract(passage, harness.run.id)

    assert rows_of(harness.factory.storage, Statement) == []
    assert rows_of(harness.factory.storage, EvidenceLink) == []
    # the extractor's session was rolled back, never committed
    assert any(s.rolled_back for s in harness.factory.sessions)
    assert all(s.committed is False for s in harness.factory.sessions)


async def test_secret_redacted_from_prompt_and_persisted_rows(harness: Harness) -> None:
    """G-05: a secret in the passage never reaches the provider or storage."""
    secret_text = f"Retail sales grew 4% last quarter. Credentials: {SECRET} were rotated."
    passage = harness.passage(text=secret_text)
    harness.provider.queue(
        FakeResponse(json.dumps({"statements": [{"text": secret_text, "confidence": 0.9}]}))
    )

    statements = await harness.extractor.extract(passage, harness.run.id)

    assert len(statements) == 1
    persisted_text = " ".join(
        str(obj.text) for obj in harness.factory.storage.values() if isinstance(obj, Statement)
    )
    assert SECRET not in persisted_text
    assert "[REDACTED_API_KEY]" in persisted_text
    # use_cache=False means the raw model output is never persisted anywhere
    assert rows_of(harness.factory.storage, KVEntry) == []
    for call in harness.provider.calls:
        for message in call["messages"]:
            assert SECRET not in str(message.get("content"))


async def test_prompt_separates_instructions_from_data_block(harness: Harness) -> None:
    """G-01: system holds instructions; the user message holds only labeled data."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse(EXTRACTION_JSON))

    await harness.extractor.extract(passage, harness.run.id)

    messages = harness.provider.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert "extract" in str(messages[0]["content"]).lower()
    assert "<passage_data" not in str(messages[0]["content"])
    assert messages[1]["role"] == "user"
    assert f'<passage_data passage_id="{passage.id}">' in str(messages[1]["content"])
    assert "<passage_data" in str(messages[1]["content"])
    # the gateway appends its own schema instruction as a final system message
    assert messages[-1]["role"] == "system"


async def test_extract_uses_cheap_tier_single_gateway_call(harness: Harness) -> None:
    """Extraction routes through tier='cheap' and never calls the provider directly."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse(EXTRACTION_JSON))

    await harness.extractor.extract(passage, harness.run.id)

    assert len(harness.provider.calls) == 1
    assert harness.provider.calls[0]["model"] == "fake/cheap-model"


async def test_empty_extraction_returns_empty_list_without_writes(harness: Harness) -> None:
    """A passage with no extractable claims yields [] and zero rows."""
    passage = harness.passage()
    harness.provider.queue(FakeResponse(json.dumps({"statements": []})))

    result = await harness.extractor.extract(passage, harness.run.id)

    assert result == []
    assert rows_of(harness.factory.storage, Statement) == []
    assert rows_of(harness.factory.storage, EvidenceLink) == []


async def test_confidence_persisted_onto_statement(harness: Harness) -> None:
    """Model-reported confidence lands on the Statement row unchanged."""
    passage = harness.passage()
    harness.provider.queue(
        FakeResponse(json.dumps({"statements": [{"text": "A claim.", "confidence": 0.42}]}))
    )

    statements = await harness.extractor.extract(passage, harness.run.id)

    assert len(statements) == 1
    assert statements[0].confidence == 0.42
    stored = rows_of(harness.factory.storage, Statement)
    assert stored[0].confidence == 0.42


async def test_empty_passage_is_rejected_before_any_llm_call(harness: Harness) -> None:
    """Degenerate input fails fast without touching the provider."""
    passage = harness.passage(text="   ")

    with pytest.raises(ValueError, match="empty passage"):
        await harness.extractor.extract(passage, harness.run.id)

    assert harness.provider.calls == []
    assert rows_of(harness.factory.storage, Statement) == []
    assert rows_of(harness.factory.storage, EvidenceLink) == []
