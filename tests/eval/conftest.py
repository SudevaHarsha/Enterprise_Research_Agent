"""Shared fixtures and hermetic harnesses for the ECRKE eval suite.

Reuses the proven fake services from ``tests.conftest`` and the
``FlowHarness`` wiring from ``tests.test_pipeline_flows`` so the eval suite
tests the same composition root the real pipeline tests use. Three harnesses:

- ``make_detector_harness`` — a real :class:`ContradictionDetector` over fake
  session + provider (guardrail + recall tests).
- ``make_collect_harness`` — a :class:`FlowHarness` with an optional custom
  fetcher (collect-stage guardrail tests).
- ``eval_api_client`` — TestClient with FakeSessionFactory overrides
  (API-level guardrail tests).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_session_factory
from app.core.config import Settings
from app.db.models import Run, Statement
from app.main import app
from app.pipeline.context import PipelineServices
from app.services.contradiction_detector import ContradictionDetector
from app.services.cost_meter import CostMeter
from app.services.fetcher import FetchedContent
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway
from tests.conftest import FakeProvider, FakeResponse, FakeSessionFactory
from tests.eval.gold import load_contradictions, load_questions
from tests.test_pipeline_flows import FakeFetcher, FlowHarness


def make_statement(text: str, run_id: UUID | str, status: str = "verified") -> Statement:
    """Build a Statement row without a database (pre-insert object)."""
    return Statement(
        id=uuid4(),
        run_id=run_id,
        passage_id=uuid4(),
        text=text,
        status=status,
    )


def flag_json(*, flag: str, contradictory: bool, reason: str = "judged") -> FakeResponse:
    """FakeProvider response for a flag judge call (ContradictionFlag schema)."""
    return FakeResponse(
        json.dumps(
            {
                "contradictory": contradictory,
                "flag": flag,
                "reason": reason,
                "confidence": 0.9,
            }
        )
    )


def confirm_json(*, contradictory: bool, reason: str = "judged") -> FakeResponse:
    """FakeProvider response for a confirm judge call (ConfirmVerdict schema)."""
    return FakeResponse(
        json.dumps(
            {
                "contradictory": contradictory,
                "reason": reason,
                "confidence": 0.9,
            }
        )
    )


def _hermetic_settings() -> Settings:
    """Hermetic settings mirroring the FlowHarness defaults (no credentials)."""
    return Settings(
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


@dataclass
class DetectorHarness:
    """Wiring for one hermetic contradiction-detector test."""

    settings: Settings
    factory: FakeSessionFactory
    cache: KVCache
    meter: CostMeter
    gateway: LLMGateway
    detector: ContradictionDetector
    provider: FakeProvider
    run: Run


def make_detector_harness(
    factory: FakeSessionFactory | None = None,
    settings: Settings | None = None,
) -> DetectorHarness:
    """Build a real ContradictionDetector over fake session + provider."""
    resolved_settings = settings if settings is not None else _hermetic_settings()
    resolved_factory = factory if factory is not None else FakeSessionFactory()
    cache = KVCache(session_factory=resolved_factory)
    meter = CostMeter(
        session_factory=resolved_factory,
        cost_fn=lambda response, model: Decimal("0.0010"),
    )
    provider = FakeProvider()
    gateway = LLMGateway(
        settings=resolved_settings,
        provider=provider,
        cache=cache,
        meter=meter,
    )
    detector = ContradictionDetector(gateway=gateway, session_factory=resolved_factory)
    run = Run(
        id=uuid4(),
        tenant_id=uuid4(),
        question="eval seed question",
        status="submitted",
        stage=None,
        progress=0.0,
        cost_budget_usd=Decimal("100.0000"),
        cost_spent_usd=Decimal("0.0000"),
    )
    resolved_factory.storage[run.id] = run
    return DetectorHarness(
        settings=resolved_settings,
        factory=resolved_factory,
        cache=cache,
        meter=meter,
        gateway=gateway,
        detector=detector,
        provider=provider,
        run=run,
    )


class RespondingFetcher(FakeFetcher):
    """FakeFetcher with a public canned-response registration method."""

    def respond(self, url: str, content: bytes, content_type: str) -> None:
        self._responses[url] = FetchedContent(url, content, content_type, datetime.now(UTC))


def make_collect_harness(
    *,
    fetcher: FakeFetcher | None = None,
    urls: list[str] | None = None,
) -> FlowHarness:
    """Build a FlowHarness with an optional custom fetcher wired into services."""
    harness = FlowHarness(urls=urls or [])
    if fetcher is None:
        return harness
    harness.fetcher = fetcher
    harness.services = PipelineServices(
        settings=harness.settings,
        session_factory=harness.factory,
        cache=harness.cache,
        meter=harness.meter,
        gateway=harness.gateway,
        planner=harness.planner,
        allowlist=harness.allowlist,
        search_connector=harness.search_connector,
        fetcher=fetcher,
        blob_store=harness.blob_store,
        normalizer=harness.normalizer,
        extractor=harness.extractor,
        verifier=harness.verifier,
        contradiction_detector=harness.detector,
        report_generator=harness.report_generator,
        audit_writer=harness.audit_writer,
    )
    return harness


@pytest.fixture(scope="session")
def gold_questions() -> list[dict[str, Any]]:
    """Session fixture: the seed questions with gold claims."""
    return load_questions()


@pytest.fixture(scope="session")
def gold_contradictions() -> list[dict[str, Any]]:
    """Session fixture: the gold contradiction pairs."""
    return load_contradictions()


@pytest.fixture
def eval_api_client() -> Iterator[tuple[TestClient, FakeSessionFactory]]:
    """TestClient with fresh FakeSessionFactory overrides (cleared after use)."""
    app.dependency_overrides.clear()
    factory = FakeSessionFactory()
    app.dependency_overrides[get_session_factory] = lambda: factory
    with TestClient(app) as client:
        yield client, factory
    app.dependency_overrides.clear()
