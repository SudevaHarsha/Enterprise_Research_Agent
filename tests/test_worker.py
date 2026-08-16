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
