"""API entrypoint.

Minimal FastAPI application. Health endpoints (liveness/readiness) are mounted
from ``app.api.health``; structured JSONL logging and the OpenTelemetry
scaffold are configured in the startup lifespan. The full evaluator-facing
surface (runs, stages, trace, conclusions, contradictions, report, audit) is
implemented in ``app.api`` (task_012).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.health import router as health_router
from app.core.logging import configure_logging
from app.core.telemetry import setup_telemetry


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    setup_telemetry()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="ECRKE - Evidence-Centric Research Knowledge Engine",
        description=(
            "Enterprise AI research agent: submit a research question, observe the "
            "10-stage pipeline, and audit every conclusion back to its source passage."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.include_router(health_router)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": "ecrke", "version": __version__}

    return app


app = create_app()
