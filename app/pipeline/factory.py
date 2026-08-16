"""Pipeline composition root (task_015 DoD fix).

The Phase-1 service bundle was previously built only inside ``app.api.deps``.
Worker-mode execution needs the same bundle built from the environment inside
the flow (when the worker receives a flow run it only carries ``run_id``), so
the composition root now lives in the pipeline layer and the API re-exports it.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.pipeline.context import PipelineServices, SessionFactory
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


def build_pipeline_services(
    settings: Settings, session_factory: SessionFactory
) -> PipelineServices:
    """Build the full Phase-1 service bundle from settings + a session factory.

    ``KVCache``/``CostMeter``/``Planner`` declare their factory parameter as
    ``async_sessionmaker``, while the caller hands them a bare ``SessionFactory``
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
