"""FastAPI dependencies: settings, session factory, and the PipelineRunner seam.

task_012 (build-plan Step 12). The API composes Phase-1 services only — it
never calls an LLM provider directly. ``build_pipeline_services`` is the
composition root the default in-process runner uses; tests override
``get_runner`` with fakes so no real services, LLM, database, or network is
ever touched by the API test suite (hermetic contract).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Protocol, cast
from uuid import UUID

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.session import async_session_factory
from app.pipeline.context import PipelineServices, SessionFactory
from app.pipeline.flows import research_pipeline, resume_pipeline
from app.services.allowlist import Allowlist
from app.services.audit_writer import AuditWriter
from app.services.blob_store import make_blob_store
from app.services.collectors.search import SearchConnector
from app.services.contradiction_detector import ContradictionDetector
from app.services.cost_meter import CostMeter
from app.services.extractor import Extractor
from app.services.fetcher import Fetcher
from app.services.kv_cache import KVCache
from app.services.llm_gateway import LLMGateway
from app.services.normalizer import Normalizer
from app.services.planner import Planner
from app.services.report_generator import ReportGenerator
from app.services.verifier import Verifier

logger = get_logger("app.api.deps")


def build_pipeline_services(
    settings: Settings, session_factory: SessionFactory
) -> PipelineServices:
    """Build the full Phase-1 service bundle (composition root).

    ``KVCache``/``CostMeter``/``Planner`` declare their factory parameter as
    ``async_sessionmaker``, while the API hands them a bare ``SessionFactory``
    callable. The cast is structural — the runtime object is callable either
    way — and keeps mypy clean.
    """
    maker = cast(async_sessionmaker[AsyncSession], session_factory)
    cache = KVCache(session_factory=maker)
    meter = CostMeter(session_factory=maker)
    gateway = LLMGateway(settings=settings, cache=cache, meter=meter)
    return PipelineServices(
        settings=settings,
        session_factory=session_factory,
        cache=cache,
        meter=meter,
        gateway=gateway,
        planner=Planner(gateway=gateway, session_factory=maker),
        allowlist=Allowlist.from_settings(settings),
        search_connector=SearchConnector.from_settings(settings),
        fetcher=Fetcher.from_settings(settings),
        blob_store=make_blob_store(settings),
        normalizer=Normalizer(),
        extractor=Extractor(gateway=gateway, session_factory=session_factory),
        verifier=Verifier(gateway=gateway, session_factory=session_factory),
        contradiction_detector=ContradictionDetector(
            gateway=gateway, session_factory=session_factory
        ),
        report_generator=ReportGenerator(gateway=gateway, session_factory=session_factory),
        audit_writer=AuditWriter(session_factory=session_factory),
    )


class PipelineRunner(Protocol):
    """Seam between the API and pipeline execution.

    ``run``/``resume`` return an outcome string (``completed`` | ``failed`` |
    ``paused``). task_013 swaps the default in-process implementation for a
    Prefect worker submission behind the same Protocol.
    """

    async def run(
        self,
        run_id: UUID | str,
        services: PipelineServices | None = None,
    ) -> str: ...

    async def resume(
        self,
        run_id: UUID | str,
        services: PipelineServices | None = None,
    ) -> str: ...


class DefaultPipelineRunner:
    """Runs the in-process Prefect flows; builds services lazily.

    Services are only constructed when a run is executed, so importing the API
    never requires a configured SEARCH_PROVIDER or LLM keys. When the
    environment cannot build the bundle (e.g. SEARCH_PROVIDER unset) or the
    flow fails, the outcome is logged and reported as ``failed`` — the run row
    is the durable artifact and stays observable; the failure is never
    re-raised to the HTTP layer (the POST already created the run, and the
    evaluator can poll it).
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        build_services: Callable[[Settings, SessionFactory], PipelineServices] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._build_services = build_services or build_pipeline_services

    async def run(
        self,
        run_id: UUID | str,
        services: PipelineServices | None = None,
    ) -> str:
        try:
            services = services or self._build_services(self._settings, self._session_factory)
        except Exception as exc:
            logger.error("pipeline_run_unavailable run_id=%s reason=%s", run_id, exc)
            return "failed"
        try:
            return await research_pipeline(run_id, services)
        except Exception as exc:
            logger.error("pipeline_run_failed run_id=%s reason=%s", run_id, exc)
            return "failed"

    async def resume(
        self,
        run_id: UUID | str,
        services: PipelineServices | None = None,
    ) -> str:
        try:
            services = services or self._build_services(self._settings, self._session_factory)
        except Exception as exc:
            logger.error("pipeline_resume_unavailable run_id=%s reason=%s", run_id, exc)
            return "failed"
        try:
            return await resume_pipeline(run_id, services)
        except Exception as exc:
            logger.error("pipeline_resume_failed run_id=%s reason=%s", run_id, exc)
            return "failed"


def get_settings_dep() -> Settings:
    """FastAPI dependency returning the process settings singleton."""
    return get_settings()


def get_session_factory() -> SessionFactory:
    """FastAPI dependency returning the async session factory.

    Tests override this dependency with a ``FakeSessionFactory``.
    """
    return async_session_factory


def get_runner(
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
) -> DefaultPipelineRunner:
    """FastAPI dependency returning the default in-process pipeline runner.

    Tests override this dependency with fake runners that record calls.
    """
    return DefaultPipelineRunner(session_factory=session_factory)
