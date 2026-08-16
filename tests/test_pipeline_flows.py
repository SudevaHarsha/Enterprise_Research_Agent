"""Pipeline flow + stage tests (task_011 — Prefect 3 DAG runner).

Hermetic: fake services + fake session only — no real LLM, DB, Docker, or
network. The stage tasks and the two @flows run IN-PROCESS inside the
session-scoped ``prefect_test_harness`` (temp Prefect API, one boot per
process). Covers the brief's flow-level acceptance points: 10-stage
end-to-end, crash-safe resume (checkpointed stages are NOT re-invoked),
cost-budget pause + alert, DB-observable progress, stage isolation (tasks
accept only PipelineContext), search/collect/store/find/trace stage behavior,
the $0 findings stage, and G-05 redaction of every persisted pipeline artifact.
"""

from __future__ import annotations

import inspect
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.db.models import (
    AuditTrace,
    Checkpoint,
    Conclusion,
    EvidenceLink,
    Finding,
    FindingStatement,
    Passage,
    Run,
    Source,
    Statement,
)
from app.pipeline.checkpoint import CheckpointStore
from app.pipeline.context import STAGE_PROGRESS, STAGES, PipelineContext, PipelineServices
from app.pipeline.flows import research_pipeline, resume_pipeline
from app.pipeline.stages.collect import run_collect
from app.pipeline.stages.find import run_find
from app.pipeline.stages.search import run_search
from app.pipeline.stages.store import run_store
from app.pipeline.stages.trace import run_trace
from app.services.allowlist import Allowlist
from app.services.audit_writer import AuditWriter
from app.services.cost_meter import CostMeter
from app.services.fetcher import FetchedContent, FetchError
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import Normalizer, content_hash, redact_secrets
from app.services.plan_schema import ResearchPlan
from app.services.report_renderer import Report
from tests.conftest import (
    FakeProvider,
    FakeSession,
    FakeSessionFactory,
    rows_of,
    sample_html_bytes,
)

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted

TRACE_TTL_DAYS = 30


# --------------------------------------------------------------------------- #
# Fake Phase-1 services (call-counting, deterministic, hermetic)
# --------------------------------------------------------------------------- #
class FakePlanner:
    """Mirrors Planner.plan: generates + persists the plan artifact."""

    def __init__(self, cache: KVCache, plan_payload: dict[str, Any]) -> None:
        self._cache = cache
        self._plan_payload = plan_payload
        self.calls = 0

    @staticmethod
    def plan_key(run_id: UUID | str) -> str:
        """Namespaced kv_cache key binding a plan to its run (mirrors Planner)."""
        return f"research_plan:{run_id}"

    async def plan(self, topic: str, run_id: UUID | str) -> ResearchPlan:
        self.calls += 1
        payload = dict(self._plan_payload)
        payload["topic"] = redact_secrets(topic)  # G-05 like the real planner
        await self._cache.set(
            key=f"research_plan:{run_id}",
            model="fake/planner",
            prompt_hash="fake",
            payload=payload,
            ttl_seconds=TRACE_TTL_DAYS * 24 * 60 * 60,
        )
        return ResearchPlan(**payload)


class FakeSearchConnector:
    """Search connector returning a canned URL list per query."""

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls
        self.calls = 0
        self.queries: list[str] = []

    async def search(self, query: str, limit: int | None = None) -> list[str]:
        self.calls += 1
        self.queries.append(query)
        return list(self._urls)


class FakeFetcher:
    """Fetcher returning canned content per URI; ``fail_urls`` raise FetchError."""

    def __init__(
        self,
        responses: dict[str, FetchedContent],
        fail_urls: set[str] | None = None,
    ) -> None:
        self._responses = responses
        self.fail_urls = set(fail_urls or ())
        self.calls = 0

    async def fetch(self, uri: str, connector: str = "default") -> FetchedContent:
        self.calls += 1
        if uri in self.fail_urls:
            raise FetchError(f"simulated fetch failure for {uri}")
        if uri in self._responses:
            return self._responses[uri]
        return FetchedContent(
            uri=uri,
            content=sample_html_bytes(),
            content_type="text/html",
            fetched_at=datetime.now(UTC),
        )


class FakeBlobStore:
    """In-memory BlobStore protocol stand-in."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.calls = 0

    async def put(self, ref: str, content: bytes) -> None:
        self.calls += 1
        self.blobs[ref] = content

    async def get(self, ref: str) -> bytes:
        self.calls += 1
        return self.blobs[ref]


class FakeExtractor:
    """Extracts one draft statement per passage (mirrors Extractor.extract)."""

    def __init__(self, factory: FakeSessionFactory) -> None:
        self._factory = factory
        self.calls = 0
        self.fail_after: int | None = None

    async def extract(self, passage: Passage, run_id: UUID | str) -> list[Statement]:
        self.calls += 1
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("simulated kill")
        statement = Statement(
            id=uuid4(),
            run_id=run_id,
            passage_id=passage.id,
            text=f"extracted statement {self.calls}",
            status="draft",
        )
        link = EvidenceLink(
            id=uuid4(),
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=run_id,
            score="none",
            method="extract",
        )
        async with self._factory() as session:
            session.add(statement)
            session.add(link)
            await session.commit()
        return [statement]


class FakeVerifier:
    """Verifies one draft statement (mirrors Verifier.verify)."""

    def __init__(self, factory: FakeSessionFactory) -> None:
        self._factory = factory
        self.calls = 0
        self.fail_after: int | None = None
        self.score = "full"

    async def verify(
        self,
        statement: Statement,
        passage: Passage,
        run_id: UUID | str,
        *,
        force: bool = False,
    ) -> Statement:
        self.calls += 1
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise RuntimeError("simulated kill")
        statement.status = "verified"
        link = EvidenceLink(
            id=uuid4(),
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=run_id,
            score=self.score,
            method="verify",
        )
        async with self._factory() as session:
            session.add(link)
            await session.commit()
        return statement


class FakeContradictionDetector:
    """Returns no contradictions (mirrors ContradictionDetector.detect)."""

    def __init__(self) -> None:
        self.calls = 0

    async def detect(self, statements: list[Statement], run_id: UUID | str) -> list[Any]:
        self.calls += 1
        return []


class FakeReportGenerator:
    """Persists one conclusion + returns a redacted Report."""

    def __init__(self, factory: FakeSessionFactory) -> None:
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
        async with self._factory() as session:
            session.add(
                Conclusion(
                    id=uuid4(),
                    run_id=run_id,
                    text=f"synthesized conclusion {self.calls}",
                    confidence=0.9,
                    human_review_required=False,
                )
            )
            await session.commit()
        return Report(
            run_id=str(run_id),
            topic=redact_secrets(topic),
            generated_at=datetime.now(UTC),
        )


# --------------------------------------------------------------------------- #
# Progress-observability session factory
# --------------------------------------------------------------------------- #
class ProgressTrackingSession(FakeSession):
    """FakeSession recording (run.stage, run.progress) on every commit."""

    def __init__(self, storage: dict[Any, Any], observed: list[tuple[str, float]]) -> None:
        super().__init__(storage)
        self._observed = observed

    async def commit(self) -> None:
        self.committed = True
        for obj in self._storage.values():
            if isinstance(obj, Run) and obj.stage is not None:
                self._observed.append((obj.stage, obj.progress))


class ProgressTrackingFactory(FakeSessionFactory):
    """Session factory whose sessions record progress observations."""

    def __init__(self, storage: dict[Any, Any] | None = None) -> None:
        super().__init__(storage)
        self.observed: list[tuple[str, float]] = []

    def __call__(self) -> ProgressTrackingSession:
        return ProgressTrackingSession(self.storage, self.observed)


# --------------------------------------------------------------------------- #
# Harness: one hermetic run + fake services wired into PipelineServices
# --------------------------------------------------------------------------- #
class FlowHarness:
    """Wiring for one hermetic pipeline test (fake services + fake session)."""

    def __init__(
        self,
        question: str = "How is AI transforming retail operations?",
        budget: Decimal | str = "100.00",
        factory: FakeSessionFactory | None = None,
        urls: list[str] | None = None,
    ) -> None:
        self.settings = Settings(
            app_env="test",
            allowed_domains="retail.example.com,retailtech.example.com",
            llm_model_cheap="fake/cheap-model",
            llm_model_strong="fake/strong-model",
            search_provider="mock",
            search_results_limit=5,
            fetch_min_interval_seconds=0.0,
            fetch_timeout_seconds=5.0,
            blob_store_backend="local",
            blob_store_dir=".blobs",
        )
        self.factory = factory if factory is not None else FakeSessionFactory()
        self.cache = KVCache(session_factory=self.factory)
        self.plan_payload = {
            "topic": question,
            "sub_questions": [
                "How are retailers adopting AI for operations?",
                "What evidence exists on AI retail transformation?",
                "What risks and costs do AI retail initiatives face?",
            ],
            "hypotheses": ["AI adoption is growing."],
            "taxonomy_hints": ["retail", "AI"],
            "source_domain_hints": ["retail.example.com"],
        }
        self.planner = FakePlanner(self.cache, self.plan_payload)
        urls = (
            urls
            if urls is not None
            else [
                "https://retail.example.com/report1",
                "https://retail.example.com/report2",
            ]
        )
        self.search_connector = FakeSearchConnector(urls)
        html = sample_html_bytes()
        now = datetime.now(UTC)
        self.fetcher = FakeFetcher(
            {
                "https://retail.example.com/report1": FetchedContent(
                    "https://retail.example.com/report1", html, "text/html", now
                ),
                "https://retail.example.com/report2": FetchedContent(
                    "https://retail.example.com/report2", html, "text/html", now
                ),
            }
        )
        self.blob_store = FakeBlobStore()
        self.normalizer = Normalizer()
        self.extractor = FakeExtractor(self.factory)
        self.verifier = FakeVerifier(self.factory)
        self.detector = FakeContradictionDetector()
        self.report_generator = FakeReportGenerator(self.factory)
        self.audit_writer = AuditWriter(session_factory=self.factory)
        self.allowlist = Allowlist(["retail.example.com", "retailtech.example.com"])
        self.provider = FakeProvider()
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
        self.run = Run(
            id=uuid4(),
            tenant_id=uuid4(),
            question=question,
            status="submitted",
            stage=None,
            progress=0.0,
            cost_budget_usd=Decimal(budget),
            cost_spent_usd=Decimal("0.0000"),
        )
        self.factory.storage[self.run.id] = self.run

    def ctx(self) -> PipelineContext:
        return PipelineContext(run_id=self.run.id, services=self.services)


# --------------------------------------------------------------------------- #
# Flow-level tests
# --------------------------------------------------------------------------- #
async def test_research_pipeline_runs_all_10_stages_end_to_end(prefect_harness: Any) -> None:
    harness = FlowHarness()
    result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    assert harness.run.status == "completed"
    assert harness.run.progress == 1.0
    assert harness.run.stage == "done"
    assert harness.run.completed_at is not None
    assert len(rows_of(harness.factory.storage, Checkpoint)) == 10
    trace = await harness.cache.get(f"trace:{harness.run.id}")
    assert trace is not None
    # every stage was invoked exactly once by the seed run
    assert harness.planner.calls == 1
    assert harness.search_connector.calls == 3
    assert harness.fetcher.calls == 2
    assert harness.extractor.calls >= 1
    assert harness.verifier.calls >= 1
    assert harness.detector.calls == 1
    assert harness.report_generator.calls == 1


async def test_kill_mid_run_then_resume_skips_checkpointed_stages(prefect_harness: Any) -> None:
    harness = FlowHarness()
    harness.verifier.fail_after = 1  # verify (stage 6) dies after stages 1-5 completed
    with pytest.raises(RuntimeError, match="simulated kill"):
        await research_pipeline(harness.run.id, harness.services)
    assert harness.run.status == "failed"
    # the failure is appended to the immutable audit trail (worker-mode parity:
    # the flow's failure handler writes run.failed, not just the API submit path)
    failed_audit = [
        a for a in rows_of(harness.factory.storage, AuditTrace) if a.action == "run.failed"
    ]
    assert len(failed_audit) == 1
    assert failed_audit[0].actor == "pipeline"
    assert failed_audit[0].decision == "failed"
    assert failed_audit[0].entity_type == "run"
    completed = {cp.stage for cp in rows_of(harness.factory.storage, Checkpoint)}
    assert completed == {"define", "search", "collect", "store", "extract"}
    counts_before = {
        "planner": harness.planner.calls,
        "search": harness.search_connector.calls,
        "fetcher": harness.fetcher.calls,
        "extractor": harness.extractor.calls,
    }
    harness.verifier.fail_after = None
    result = await resume_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    assert harness.run.status == "completed"
    assert harness.run.progress == 1.0
    # stages 1-5 were NOT re-invoked on resume
    assert harness.planner.calls == counts_before["planner"]
    assert harness.search_connector.calls == counts_before["search"]
    assert harness.fetcher.calls == counts_before["fetcher"]
    assert harness.extractor.calls == counts_before["extractor"]
    # seed: 1 failed attempt (fail_after=1) + resume: 2 drafts verified
    assert harness.verifier.calls == 3


async def test_budget_breach_pauses_then_resume_with_raised_budget_completes(
    prefect_harness: Any, caplog: Any
) -> None:
    harness = FlowHarness(budget="5.00")  # stage budget = 0.50
    harness.run.cost_spent_usd = Decimal("0.60")  # already over the stage budget
    with caplog.at_level(logging.ERROR):
        result = await research_pipeline(harness.run.id, harness.services)
    assert result == "paused"
    assert harness.run.status == "paused"
    assert any("circuit_breaker_open" in r.getMessage() for r in caplog.records)
    completed = {cp.stage for cp in rows_of(harness.factory.storage, Checkpoint)}
    assert completed == {"define"}
    harness.run.cost_budget_usd = Decimal("100.00")
    result2 = await resume_pipeline(harness.run.id, harness.services)
    assert result2 == "completed"
    assert harness.run.status == "completed"
    assert harness.planner.calls == 1  # define skipped on resume
    assert harness.search_connector.calls == 3


async def test_progress_observable_via_db_after_each_stage(prefect_harness: Any) -> None:
    harness = FlowHarness(factory=ProgressTrackingFactory())
    result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    expected = {(stage, STAGE_PROGRESS[stage]) for stage in STAGES}
    observed = set(harness.factory.observed)
    assert expected <= observed


async def test_stage_tasks_receive_only_pipeline_context(prefect_harness: Any) -> None:
    from app.pipeline.stages import (
        collect,
        conclude,
        define,
        detect,
        extract,
        find,
        search,
        store,
        trace,
        verify,
    )

    tasks = {
        "define": define.run_define,
        "search": search.run_search,
        "collect": collect.run_collect,
        "store": store.run_store,
        "extract": extract.run_extract,
        "verify": verify.run_verify,
        "find": find.run_find,
        "detect": detect.run_detect,
        "conclude": conclude.run_conclude,
        "trace": trace.run_trace,
    }
    for stage, task in tasks.items():
        fn = getattr(task, "fn", task)
        params = list(inspect.signature(fn).parameters)
        assert params == ["ctx"], f"{stage} task must accept only ctx"


# --------------------------------------------------------------------------- #
# Stage-level tests (tasks awaited directly — in-process, no server)
# --------------------------------------------------------------------------- #
async def test_search_derives_queries_from_plan_artifact(prefect_harness: Any) -> None:
    harness = FlowHarness()
    await harness.planner.plan(harness.run.question, harness.run.id)  # seed artifact
    result = await run_search(harness.ctx())
    assert result.ok
    assert result.detail["urls"] == [
        "https://retail.example.com/report1",
        "https://retail.example.com/report2",
    ]
    assert harness.search_connector.calls == 3
    assert harness.search_connector.queries == harness.plan_payload["sub_questions"]


async def test_search_missing_plan_raises_before_any_connector_call(
    prefect_harness: Any,
) -> None:
    harness = FlowHarness()  # plan never written to kv_cache
    with pytest.raises(ValueError, match="research_plan"):
        await run_search(harness.ctx())
    assert harness.search_connector.calls == 0


async def test_collect_handles_allowlist_fetch_and_failure_statuses(
    prefect_harness: Any,
) -> None:
    harness = FlowHarness()
    allowed = "https://retail.example.com/good"
    denied = "https://evil.example.com/bad"
    failed = "https://retail.example.com/broken"
    store = CheckpointStore(harness.factory)
    await store.save(harness.run.id, "search", {"urls": [allowed, denied, failed]})
    harness.fetcher.fail_urls.add(failed)
    result = await run_collect(harness.ctx())
    assert result.ok
    sources = {s.uri: s for s in rows_of(harness.factory.storage, Source)}
    assert sources[allowed].status == "fetched"
    assert sources[allowed].raw_ref is not None
    assert sources[allowed].allowlisted_uri is True
    assert sources[denied].status == "quarantined"
    assert sources[denied].raw_ref is None
    assert sources[denied].allowlisted_uri is False
    assert sources[failed].status == "failed"
    assert harness.blob_store.calls == 1  # only the allowed source was stored
    actions = {a.action for a in rows_of(harness.factory.storage, AuditTrace)}
    assert "source.quarantined" in actions
    assert "source.fetch_failed" in actions


async def test_store_normalizes_and_chunks_sources_into_passages(
    prefect_harness: Any,
) -> None:
    harness = FlowHarness()
    source = Source(
        id=uuid4(),
        run_id=harness.run.id,
        uri="https://retail.example.com/report1",
        title="Retail report",
        source_type="web",
        content_hash=content_hash(sample_html_bytes()),
        raw_ref="run/1/src/1",
        allowlisted_uri=True,
        status="fetched",
    )
    harness.factory.storage[source.id] = source
    harness.blob_store.blobs[source.raw_ref] = sample_html_bytes()
    result = await run_store(harness.ctx())
    assert result.ok
    passages = rows_of(harness.factory.storage, Passage)
    assert len(passages) >= 1
    for passage in passages:
        assert passage.source_id == source.id
        assert passage.seq == passages.index(passage)
        assert passage.hash
        assert passage.text
    assert source.status == "normalized"


async def test_find_groups_verified_statements_by_domain_with_tiers_no_llm(
    prefect_harness: Any,
) -> None:
    harness = FlowHarness()
    run_id = harness.run.id

    def source(uri: str, cid: str) -> Source:
        row = Source(
            id=uuid4(),
            run_id=run_id,
            uri=uri,
            source_type="web",
            content_hash=cid,
            status="normalized",
        )
        harness.factory.storage[row.id] = row
        return row

    def passage(src: Source) -> Passage:
        row = Passage(id=uuid4(), source_id=src.id, seq=0, text="evidence", hash="ph")
        harness.factory.storage[row.id] = row
        return row

    def statement(passage: Passage, text: str) -> Statement:
        row = Statement(
            id=uuid4(),
            run_id=run_id,
            passage_id=passage.id,
            text=text,
            status="verified",
        )
        harness.factory.storage[row.id] = row
        return row

    src_a = source("https://retail.example.com/a", "h1")
    src_b = source("https://retailtech.example.com/b", "h2")
    src_c = source("https://other.example.com/c", "h3")
    pas_a = passage(src_a)
    pas_b = passage(src_b)
    pas_c = passage(src_c)
    s1 = statement(pas_a, "full in retail")
    s2 = statement(pas_b, "full in tech")
    s3 = statement(pas_a, "partial in retail")
    statement(pas_c, "no verify link")
    for link in [
        EvidenceLink(
            id=uuid4(),
            statement_id=s1.id,
            passage_id=pas_a.id,
            run_id=run_id,
            score="full",
            method="verify",
        ),
        EvidenceLink(
            id=uuid4(),
            statement_id=s2.id,
            passage_id=pas_b.id,
            run_id=run_id,
            score="full",
            method="verify",
        ),
        EvidenceLink(
            id=uuid4(),
            statement_id=s3.id,
            passage_id=pas_a.id,
            run_id=run_id,
            score="partial",
            method="verify",
        ),
    ]:
        harness.factory.storage[link.id] = link

    result = await run_find(harness.ctx())
    assert result.ok
    findings = rows_of(harness.factory.storage, Finding)
    assert len(findings) == 3
    by_domain = {f.domain_tags[0]: f.evidence_tier for f in findings}
    assert by_domain["retail.example.com"] == "t1"
    assert by_domain["retailtech.example.com"] == "t1"
    assert by_domain["other.example.com"] == "t3"
    assert len(rows_of(harness.factory.storage, FindingStatement)) == 4
    assert harness.provider.calls == []  # $0 stage


async def test_trace_exports_audit_rows_and_report_bundle_with_30d_ttl(
    prefect_harness: Any,
) -> None:
    harness = FlowHarness()
    await harness.audit_writer.record(
        run_id=harness.run.id,
        entity_type="statement",
        entity_id="s1",
        action="statement.verify",
        actor="pipeline",
        decision="verified",
        reason="matrix full",
    )
    await harness.audit_writer.record(
        run_id=harness.run.id,
        entity_type="source",
        entity_id="s2",
        action="source.fetched",
        actor="pipeline",
        decision="fetched",
    )
    store = CheckpointStore(harness.factory)
    await store.save(
        harness.run.id,
        "conclude",
        {"report": {"run_id": str(harness.run.id), "topic": "AI retail", "conclusions": []}},
    )
    before = datetime.now(UTC)
    result = await run_trace(harness.ctx())
    after = datetime.now(UTC)
    assert result.ok
    entry = harness.factory.storage.get(f"trace:{harness.run.id}")
    assert entry is not None
    assert entry.expires_at is not None
    assert before + timedelta(days=TRACE_TTL_DAYS) - timedelta(seconds=5) <= entry.expires_at
    assert entry.expires_at <= after + timedelta(days=TRACE_TTL_DAYS) + timedelta(seconds=5)
    payload = await harness.cache.get(f"trace:{harness.run.id}")
    assert payload is not None
    assert len(payload["audit_trace"]) == 2
    assert payload["report"]["topic"] == "AI retail"
    assert payload["run_id"] == str(harness.run.id)


async def test_g05_no_secret_in_pipeline_artifacts_or_logs(
    prefect_harness: Any, caplog: Any
) -> None:
    harness = FlowHarness(question=f"How is AI transforming retail? use {SECRET} now")
    with caplog.at_level(logging.ERROR):
        result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    for checkpoint in rows_of(harness.factory.storage, Checkpoint):
        assert SECRET not in json.dumps(checkpoint.state)
    assert SECRET not in json.dumps(harness.run.checkpoint)
    trace = await harness.cache.get(f"trace:{harness.run.id}")
    assert trace is not None
    assert SECRET not in json.dumps(trace)
    for record in caplog.records:
        assert SECRET not in record.getMessage()
