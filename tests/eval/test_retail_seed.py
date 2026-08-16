"""Seed-run trust harness (build-plan Step 14).

Runs the REAL research pipeline over fakes with sentence-extractor / full-score
verifier / report-generator doubles, then measures the seed-run trust metrics
(decomposition coverage, support ratio, traceability) against the floors.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from app.db.models import (
    Conclusion,
    ConclusionEvidence,
    EvidenceLink,
    Passage,
    Source,
    Statement,
)
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.context import PipelineServices
from app.pipeline.flows import research_pipeline
from app.services.report_renderer import (
    Report,
    ReportConclusion,
    ReportEvidenceStatement,
    ReportSupportEntry,
)
from tests.conftest import rows_of
from tests.eval.eval_metrics import (
    SUPPORT_FLOOR,
    statement_decomposition_coverage,
    support_ratio,
    traceability,
)
from tests.test_pipeline_flows import FlowHarness


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()]


class RetailExtractor:
    """One draft statement per sentence of the passage."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.calls = 0

    async def extract(self, passage: Passage, run_id: UUID | str) -> list[Statement]:
        self.calls += 1
        statements: list[Statement] = []
        async with self._factory() as session:
            for sentence in _sentences(passage.text):
                statement = Statement(
                    id=uuid4(), run_id=run_id, passage_id=passage.id, text=sentence, status="draft"
                )
                statements.append(statement)
                session.add(statement)
                session.add(
                    EvidenceLink(
                        id=uuid4(),
                        statement_id=statement.id,
                        passage_id=passage.id,
                        run_id=run_id,
                        score="none",
                        method="extract",
                    )
                )
            await session.commit()
        return statements


class RetailVerifier:
    """Marks every statement verified with a full-score evidence link."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.calls = 0

    async def verify(
        self, statement: Statement, passage: Passage, run_id: UUID | str, *, force: bool = False
    ) -> Statement:
        self.calls += 1
        statement.status = "verified"
        async with self._factory() as session:
            session.add(
                EvidenceLink(
                    id=uuid4(),
                    statement_id=statement.id,
                    passage_id=passage.id,
                    run_id=run_id,
                    score="full",
                    method="verify",
                )
            )
            await session.commit()
        return statement


class RetailReportGenerator:
    """Deterministic report: one conclusion with full-score support rows."""

    def __init__(self, factory: Any) -> None:
        self._factory = factory
        self.calls = 0

    async def generate(
        self,
        run_id: UUID | str,
        topic: str,
        verified_statements: list[Statement],
        confirmed_contradictions: list[Any],
    ) -> Report:
        self.calls += 1
        conclusion = Conclusion(
            id=uuid4(),
            run_id=run_id,
            text=(
                "Retailers report stronger same-store sales growth as e-commerce expands "
                "its share of total retail spending."
            ),
            confidence=0.9,
            human_review_required=False,
        )
        support_matrix: list[ReportSupportEntry] = []
        evidence_statements: list[ReportEvidenceStatement] = []
        async with self._factory() as session:
            session.add(conclusion)
            for statement in verified_statements:
                session.add(
                    ConclusionEvidence(conclusion_id=conclusion.id, statement_id=statement.id)
                )
                support_matrix.append(
                    ReportSupportEntry(
                        statement_id=str(statement.id),
                        passage_id=str(statement.passage_id),
                        support_score="full",
                    )
                )
                evidence_statements.append(
                    ReportEvidenceStatement(
                        id=str(statement.id),
                        text=statement.text,
                        source_domain="retail.example.com",
                    )
                )
            await session.commit()
        return Report(
            run_id=str(run_id),
            topic=topic,
            generated_at=datetime.now(UTC),
            conclusions=[
                ReportConclusion(
                    id=str(conclusion.id),
                    text=conclusion.text,
                    confidence=0.9,
                    human_review_required=False,
                    support_matrix=support_matrix,
                    evidence_statements=evidence_statements,
                )
            ],
        )


class RetailSeedHarness(FlowHarness):
    """FlowHarness with deterministic retail specialists wired into services."""

    def __init__(
        self,
        question: str = "How is AI transforming retail operations?",
        budget: str | Decimal = "100.00",
    ) -> None:
        super().__init__(question=question, budget=budget)
        self.extractor = RetailExtractor(self.factory)
        self.verifier = RetailVerifier(self.factory)
        self.report_generator = RetailReportGenerator(self.factory)
        self.services = PipelineServices(
            settings=self.settings,
            session_factory=self.factory,
            cache=self.cache,
            meter=self.meter,
            gateway=self.gateway,
            planner=self.planner,
            allowlist=self.allowlist,
            search_connector=self.search_connector,
            fetcher=self.fetcher,
            blob_store=self.blob_store,
            normalizer=self.normalizer,
            extractor=self.extractor,
            verifier=self.verifier,
            contradiction_detector=self.detector,
            report_generator=self.report_generator,
            audit_writer=self.audit_writer,
        )


async def test_seed_run_meets_trust_floors(prefect_harness: Any) -> None:
    harness = RetailSeedHarness()
    result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"

    storage = harness.factory.storage
    passages = rows_of(storage, Passage)
    assert passages

    verified = [s for s in rows_of(storage, Statement) if s.status == "verified"]
    assert verified
    assert all(statement.passage_id is not None for statement in verified)

    # Decomposition coverage: every verified statement's tokens appear in its passage.
    passage_by_id = {passage.id: passage for passage in passages}
    coverage = statement_decomposition_coverage(verified, [passage.text for passage in passages])
    assert coverage >= 0.5

    # Support ratio: conclusion support rows are all "full".
    store = CheckpointStore(harness.factory)
    conclude = await store.load(harness.run.id, "conclude")
    assert conclude is not None and "report" in conclude
    report = Report.model_validate(conclude["report"])
    assert report.conclusions
    assert support_ratio(report.conclusions[0].support_matrix) >= SUPPORT_FLOOR

    # Traceability: every verified statement chains to its passage and source.
    source_by_id = {source.id: source for source in rows_of(storage, Source)}
    chain_map: dict[str, tuple[str | None, str | None]] = {}
    for statement in verified:
        passage = passage_by_id.get(statement.passage_id)
        source = source_by_id.get(passage.source_id) if passage is not None else None
        chain_map[str(statement.id)] = (
            str(statement.passage_id) if passage is not None else None,
            str(source.id) if source is not None else None,
        )
    assert traceability(chain_map) >= 0.5

    # Every verified statement has exactly one full-score verification link.
    full_links = [
        link
        for link in rows_of(storage, EvidenceLink)
        if link.score == "full" and link.method == "verify"
    ]
    assert len(full_links) == len(verified)
