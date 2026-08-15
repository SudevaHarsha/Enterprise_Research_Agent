"""Liveness and readiness endpoints (mounted in ``app.main``).

``/healthz`` answers liveness (the process is alive); ``/readyz`` answers
readiness and reports the service version. Later steps may enrich ``readyz``
with dependency probes (Postgres, Prefect) without changing the contract.
"""

from __future__ import annotations

from fastapi import APIRouter

from app import __version__

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    """Return 200 when the process is alive."""
    return {"status": "ok"}


@router.get("/readyz", summary="Readiness probe")
async def readyz() -> dict[str, str]:
    """Return 200 with the service version when ready to serve traffic."""
    return {"status": "ready", "version": __version__}
