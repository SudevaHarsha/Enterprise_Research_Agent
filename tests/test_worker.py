"""Unit tests for ``app.workers.worker`` (task_013 — Prefect deployment config).

Hermetic: builds the ``RunnerDeployment`` object from the in-process flow
object — no Prefect server, no Docker, no network. The YAML round-trip uses
the deployment's own JSON-mode dump, so what is documented is exactly what
``prefect deploy`` would apply.
"""

from __future__ import annotations

import yaml
from prefect.deployments.runner import RunnerDeployment

from app.workers import worker


def test_deploy_research_pipeline_builds_deployment() -> None:
    deployment = worker.deploy_research_pipeline()
    assert deployment.flow_name == "research-pipeline"
    assert deployment.name == "research-worker"
    assert deployment.work_pool_name == "research"
    assert deployment.tags  # non-empty tags
    assert deployment.description  # non-empty description
    # Entrypoint is pinned to the repo-relative script path so the worker can
    # load the flow inside the container (pip-installed module paths break).
    assert deployment.entrypoint == "app/pipeline/flows.py:research_pipeline"
    # Absolute working directory for local filesystem deployments (worker CWDs
    # there before importing the entrypoint).
    assert deployment._path == "/app"


def test_deployment_applies_to_prefect_server(prefect_harness: object) -> None:
    """The registration bootstrap (compose ``register`` service) actually lands
    the deployment on the server so the worker can pick runs up (DoD fix)."""
    import asyncio

    from prefect.client.orchestration import get_client
    from prefect.client.schemas.actions import WorkPoolCreate

    async def _register() -> None:
        async with get_client() as client:
            pools = await client.read_work_pools()
            pool = next((p for p in pools if p.name == worker.WORK_POOL_NAME), None)
            if pool is None:
                await client.create_work_pool(
                    WorkPoolCreate(name=worker.WORK_POOL_NAME, type="process")
                )
            deployment = worker.deploy_research_pipeline()
            deployment_id = await deployment.apply(work_pool_name=worker.WORK_POOL_NAME)
            assert deployment_id is not None
            fetched = await client.read_deployment_by_name(
                f"{worker.FLOW_NAME}/{worker.DEPLOYMENT_NAME}"
            )
            assert fetched is not None
            assert fetched.name == worker.DEPLOYMENT_NAME

    asyncio.run(_register())


def test_deployment_yaml_round_trips() -> None:
    deployment = worker.deploy_research_pipeline()
    document = worker.deployment_to_yaml(deployment)
    parsed = yaml.safe_load(document)
    assert parsed is not None
    assert parsed["flow_name"] == "research-pipeline"
    assert parsed["work_pool_name"] == "research"
    rebuilt = worker.deployment_from_yaml(document)
    assert isinstance(rebuilt, RunnerDeployment)
    assert rebuilt.flow_name == deployment.flow_name
    assert rebuilt.name == deployment.name
    assert rebuilt.work_pool_name == deployment.work_pool_name
    assert rebuilt.tags == deployment.tags


def test_worker_entrypoint_documents_compose_command() -> None:
    assert "prefect worker start --pool research" in (worker.main.__doc__ or "")


def test_module_documents_postgres_backed_queue() -> None:
    """Queue state lives in Prefect's Postgres DB — no Redis dependency."""
    assert "Postgres" in (worker.__doc__ or "")
