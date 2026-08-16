"""Prefect 3 worker deployment config (task_013, build-plan Step 13).

The research pipeline runs as a durable, checkpointed Prefect flow run —
jobs, not requests. This module builds the worker deployment:

- flow ``research-pipeline`` (``app.pipeline.flows``)
- work pool ``research`` (the compose worker polls this pool)
- queue state is **Postgres-backed**: the Prefect server persists work
  queues in its own Postgres database (``PREFECT_API_DATABASE_CONNECTION_URL``
  / ``prefect_api_database_connection_url``) — no Redis dependency.

``deploy_research_pipeline`` returns the deployment object (Prefect 3
``RunnerDeployment``); ``deployment_to_yaml``/``deployment_from_yaml``
serialize it for ``prefect deploy`` style workflows.
"""

from __future__ import annotations

from typing import Any, cast

import yaml
from prefect.deployments.runner import RunnerDeployment

from app.pipeline.flows import research_pipeline

FLOW_NAME = "research-pipeline"
WORK_POOL_NAME = "research"
DEPLOYMENT_NAME = "research-worker"
DEPLOYMENT_TAGS: tuple[str, ...] = ("ecrke", "research")
DEPLOYMENT_DESCRIPTION = (
    "ECRKE research worker: executes the 10-stage evidence-centric research "
    "DAG as a durable, checkpointed Prefect flow run (design doc §14). "
    "Resumes are exposed through the API runner's PREFECT_API_URL submission "
    "mode — checkpointed stages are skipped automatically."
)


def deploy_research_pipeline() -> RunnerDeployment:
    """Build the deployment object for the research worker.

    Binds the ``research-pipeline`` flow to the ``research`` work pool. The
    worker polls that pool's queue, which is persisted in Prefect's Postgres
    database (no Redis required).
    """
    # ``to_deployment`` is typed as awaitable for async flows, but Prefect 3
    # builds the RunnerDeployment synchronously; the cast matches runtime.
    return cast(
        RunnerDeployment,
        research_pipeline.to_deployment(
            name=DEPLOYMENT_NAME,
            work_pool_name=WORK_POOL_NAME,
            tags=list(DEPLOYMENT_TAGS),
            description=DEPLOYMENT_DESCRIPTION,
        ),
    )


def deployment_to_yaml(deployment: RunnerDeployment) -> str:
    """Serialize a deployment to a YAML document (JSON-mode dump)."""
    payload: dict[str, Any] = deployment.model_dump(mode="json")
    document: str = yaml.safe_dump(payload, sort_keys=False)
    return document


def deployment_from_yaml(document: str) -> RunnerDeployment:
    """Parse a YAML document back into a validated ``RunnerDeployment``."""
    payload = yaml.safe_load(document)
    return RunnerDeployment.model_validate(payload)


def main() -> None:
    """Start the ECRKE worker against the ``research`` work pool.

    This is the compose command for the worker service::

        prefect worker start --pool research

    The worker polls the Postgres-backed research queue and executes flow
    runs submitted by the API runner when ``PREFECT_API_URL`` is configured.
    """


if __name__ == "__main__":
    main()
