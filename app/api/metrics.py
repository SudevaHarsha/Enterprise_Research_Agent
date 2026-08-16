"""Prometheus exposition endpoint (task_013, design doc §14).

``GET /metrics`` renders the live module registry from ``app.core.metrics``
in Prometheus text format. Prometheus scrapes this endpoint (docker-compose
``observability`` profile scrapes ``api:8000/metrics``); Grafana dashboards
query the scraped series.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.metrics import get_registry

router = APIRouter(tags=["observability"])


@router.get("/metrics", summary="Prometheus metrics exposition")
async def metrics() -> Response:
    """Return all ECRKE metrics in Prometheus text format."""
    return Response(
        content=generate_latest(get_registry()),
        media_type=CONTENT_TYPE_LATEST,
    )
