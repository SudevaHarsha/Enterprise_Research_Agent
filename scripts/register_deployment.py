"""Register the ECRKE research worker deployment against the Prefect server.

One-shot bootstrap for the compose stack (the ``register`` service): builds
the ``research-pipeline/research-worker`` RunnerDeployment and applies it to
the ``research`` work pool that the compose worker polls. The API runner
submits flow runs to the Prefect server when ``PREFECT_API_URL`` is set;
without this registration the worker would poll an empty pool and no run
would ever execute (fresh-clone DoD finding closed by task_015 verification).

Idempotent: an existing deployment is deleted before re-applying so that
fresh-clone DoD wiring changes (working directory ``path``, entrypoint, tags)
always land — the Prefect server upsert updates the entrypoint but leaves
``path`` at its original value.

Requires ``PREFECT_API_URL`` to point at the Prefect server.
"""

from __future__ import annotations

import asyncio

from prefect.client.orchestration import get_client
from prefect.client.schemas.actions import WorkPoolCreate

from app.workers.worker import (
    DEPLOYMENT_NAME,
    FLOW_NAME,
    WORK_POOL_NAME,
    deploy_research_pipeline,
)


async def _ensure_work_pool() -> None:
    """Create the ``research`` work pool if it does not exist yet.

    ``RunnerDeployment.apply(work_pool_name=...)`` requires the pool to already
    exist on the server; the compose worker auto-creates it on first start, but
    registration runs before the worker boots, so the pool is created here.
    Retries the server connection briefly — the compose healthcheck gates the
    container, but the API can still be warming up on first boot.
    """
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            async with get_client() as client:
                pools = await client.read_work_pools()
                pool = next((p for p in pools if p.name == WORK_POOL_NAME), None)
                if pool is None:
                    await client.create_work_pool(
                        WorkPoolCreate(name=WORK_POOL_NAME, type="process")
                    )
                    print(f"created work pool {WORK_POOL_NAME} (type=process)")
                else:
                    print(f"work pool {WORK_POOL_NAME} already exists")
                return
        except Exception as exc:  # connection refused while server warms up
            last_error = exc
            print(f"prefect api not ready (attempt {attempt + 1}/5): {exc}")
            await asyncio.sleep(3)
    raise RuntimeError(f"prefect api unreachable after retries: {last_error}")


async def _delete_existing_deployment() -> None:
    """Remove a previously-registered deployment if present.

    The Prefect server upsert leaves the deployment ``path`` (flow-run working
    directory) untouched when re-applying, which breaks the worker's ability to
    load the entrypoint after the working directory changes. Delete-then-apply
    guarantees the deployed definition always matches the current worker code.
    """
    try:
        async with get_client() as client:
            deployment = await client.read_deployment_by_name(f"{FLOW_NAME}/{DEPLOYMENT_NAME}")
            await client.delete_deployment(deployment.id)
        print(f"deleted existing deployment {FLOW_NAME}/{DEPLOYMENT_NAME}")
    except Exception as exc:  # noqa: BLE001 - missing deployment is expected
        print(f"no existing deployment to delete: {exc}")


async def _apply_deployment() -> None:
    deployment = deploy_research_pipeline()
    deployment_id = await deployment.apply(work_pool_name=WORK_POOL_NAME)
    print(f"registered deployment {DEPLOYMENT_NAME} id={deployment_id}")


def main() -> None:
    asyncio.run(_ensure_work_pool())
    asyncio.run(_delete_existing_deployment())
    asyncio.run(_apply_deployment())


if __name__ == "__main__":
    main()
