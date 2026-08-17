"""Pipeline orchestration contracts (task_011).

Shared vocabulary for the 10-stage Prefect DAG:

- :data:`STAGES` — canonical stage order (the pipeline DAG).
- :data:`STAGE_STATUS` — the ``runs.status`` value surfaced while a stage runs
  (observable lifecycle, design doc §5.1).
- :data:`STAGE_PROGRESS` — the ``runs.progress`` value written at stage start,
  so a stalled run is visible as a frozen progress bar in the runs table.
- :class:`PipelineServices` — the service bundle injected into every stage
  (composition root; the flow test harness wires fakes here).
- :class:`PipelineContext` — the ONLY argument a stage task may receive
  (stage-isolation guardrail: tasks never reach for globals or extra params).
- :class:`StageResult` — the JSON-serializable outcome every stage returns,
  checkpointed after the stage commits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.services.allowlist import Allowlist
from app.services.audit_writer import AuditWriter
from app.services.blob_store import BlobStore
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

# Canonical pipeline DAG — index == execution order.
STAGES: tuple[str, ...] = (
    "define",
    "search",
    "collect",
    "store",
    "extract",
    "verify",
    "find",
    "detect",
    "conclude",
    "trace",
)

# runs.status surfaced while each stage executes (design doc §5.1 lifecycle).
STAGE_STATUS: dict[str, str] = {
    "define": "planning",
    "search": "searching",
    "collect": "collecting",
    "store": "storing",
    "extract": "extracting",
    "verify": "verifying",
    "find": "comparing",
    "detect": "detecting",
    "conclude": "concluding",
    "trace": "tracing",
}

# runs.progress written at stage start: evenly spaced 0.1 -> 1.0.
STAGE_PROGRESS: dict[str, float] = {
    stage: round((index + 1) / len(STAGES), 2) for index, stage in enumerate(STAGES)
}

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True)
class PipelineServices:
    """Service bundle handed to every stage (composition root).

    Stage tasks read ONLY from ``PipelineContext.services``; the bundle is
    built once by the caller (production wiring or the flow-test harness with
    fake Phase-1 services) and never mutated by a stage.
    """

    settings: Settings
    session_factory: SessionFactory
    cache: KVCache
    meter: CostMeter
    gateway: LLMGateway
    planner: Planner
    allowlist: Allowlist
    search_connector: SearchConnector
    fetcher: Fetcher
    blob_store: BlobStore
    normalizer: Normalizer
    extractor: Extractor
    verifier: Verifier
    contradiction_detector: ContradictionDetector
    report_generator: ReportGenerator
    audit_writer: AuditWriter


@dataclass(frozen=True)
class PipelineContext:
    """Immutable per-run context passed to every stage task.

    ``run_id`` plus the shared :class:`PipelineServices` bundle. Stage tasks
    accept exactly one parameter: ``ctx`` (stage-isolation guardrail).
    """

    run_id: UUID | str
    services: PipelineServices

    @property
    def session_factory(self) -> SessionFactory:
        """Convenience: the bundle's session factory."""
        return self.services.session_factory

    def __hash__(self) -> int:
        """Hash on run_id only so Prefect task caching can serialize this."""
        return hash(str(self.run_id))


@dataclass(frozen=True)
class StageResult:
    """JSON-serializable outcome of one stage, checkpointed after commit."""

    stage: str
    ok: bool
    detail: dict[str, Any] | None = field(default=None)
