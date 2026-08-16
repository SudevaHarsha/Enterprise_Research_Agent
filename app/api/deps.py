"""FastAPI dependencies: settings, session factory, and the PipelineRunner seam.

task_012 (build-plan Step 12). The API composes Phase-1 services only — it
never calls an LLM provider directly. ``build_pipeline_services`` is the
composition root the default in-process runner uses; tests override
``get_runner`` with fakes so no real services, LLM, database, or network is
ever touched by the API test suite (hermetic contract).

task_013 (build-plan Step 13): when ``PREFECT_API_URL`` is set, the default
runner submits runs to the Prefect worker queue (Postgres-backed, no Redis)
instead of executing in-process. The submission reads the deployed worker
deployment by name and creates a flow run with ``{"run_id": ...}``; the
worker then executes the durable, checkpointed flow (design doc §14).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Depends
from prefect.client.orchestration import PrefectClient

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.enums import RunStatus
from app.db.models import AuditTrace, Run
from app.db.session import async_session_factory
from app.pipeline.context import PipelineServices, SessionFactory
from app.pipeline.factory import build_pipeline_services
from app.pipeline.flows import research_pipeline, resume_pipeline
from app.services.normalizer import redact_secrets
from app.workers.worker import DEPLOYMENT_NAME, FLOW_NAME

logger = get_logger("app.api.deps")


def _make_prefect_client(api_url: str) -> PrefectClient:
    """Create a ``PrefectClient`` bound to the configured API URL.

    Exposed as a module-level factory so tests can monkeypatch it with an
    in-memory stand-in (hermetic contract — no real Prefect server).
    """
    return PrefectClient(api=api_url)


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
        client_factory: Callable[[str], PrefectClient] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings or get_settings()
        self._build_services = build_services or build_pipeline_services
        self._client_factory = client_factory or _make_prefect_client

    def _submitting(self) -> bool:
        """True when the runner should hand runs to the Prefect worker queue."""
        return bool(self._settings.prefect_api_url)

    async def _persist_start_failure(self, run_id: UUID | str, reason: str) -> None:
        """Persist a terminal ``failed`` state when a run could not start.

        The durable run row is the observable contract: if the pipeline can
        never start (unconfigured SEARCH_PROVIDER/LLM keys, worker submission
        error), the run must transition out of ``submitted`` so an evaluator
        never sees a permanently-stuck run (DoD finding from task_015
        verification). Only rows still in ``submitted`` are transitioned — a
        flow that already persisted its own terminal state is left untouched,
        so no duplicate audit rows are ever written.
        """
        try:
            async with self._session_factory() as session:
                db_run = await session.get(Run, run_id)
                if db_run is None or db_run.status != RunStatus.SUBMITTED.value:
                    return
                db_run.status = RunStatus.FAILED.value
                db_run.updated_at = datetime.now(UTC)
                session.add(
                    AuditTrace(
                        run_id=db_run.id,
                        entity_type="run",
                        entity_id=str(db_run.id),
                        action="run.failed",
                        actor="pipeline",
                        decision="failed",
                        reason=redact_secrets(str(reason))[:2000],
                    )
                )
                await session.commit()
        except Exception as exc:  # pragma: no cover - defensive; never mask start failure
            logger.error("pipeline_failure_persist_error run_id=%s reason=%s", run_id, exc)

    async def _submit(self, run_id: UUID | str) -> str:
        """Submit a run to the deployed worker (``research-pipeline/research-worker``).

        Creates a flow run in Prefect's Postgres-backed queue with the run id
        passed as the flow parameter; the worker executes it and emits
        lifecycle events + metrics. Errors are logged and reported as
        ``failed`` so the run row stays observable (never re-raised).
        """
        deployment_name = f"{FLOW_NAME}/{DEPLOYMENT_NAME}"
        try:
            async with self._client_factory(self._settings.prefect_api_url) as client:
                deployment = await client.read_deployment_by_name(deployment_name)
                flow_run = await client.create_flow_run_from_deployment(
                    deployment.id,
                    parameters={"run_id": str(run_id)},
                )
            state = flow_run.state
            state_name = state.name if state is not None else "unknown"
            return state_name or "unknown"
        except Exception as exc:
            logger.error("pipeline_submit_failed run_id=%s reason=%s", run_id, exc)
            await self._persist_start_failure(run_id, str(exc))
            return "failed"

    async def run(
        self,
        run_id: UUID | str,
        services: PipelineServices | None = None,
    ) -> str:
        if self._submitting():
            return await self._submit(run_id)
        try:
            services = services or self._build_services(self._settings, self._session_factory)
        except Exception as exc:
            logger.error("pipeline_run_unavailable run_id=%s reason=%s", run_id, exc)
            await self._persist_start_failure(run_id, str(exc))
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
        if self._submitting():
            return await self._submit(run_id)
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
