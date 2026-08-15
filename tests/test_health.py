"""Unit tests for the health router (``app.api.health``) and its wiring.

Covers: ``GET /healthz`` liveness, ``GET /readyz`` readiness with service
version, and router mounting on the FastAPI app in ``app.main``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__
from app.main import app


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_readiness_and_version() -> None:
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["version"] == __version__


def test_health_router_mounted_on_app() -> None:
    paths = {route.path for route in app.routes}
    assert "/healthz" in paths
    assert "/readyz" in paths
