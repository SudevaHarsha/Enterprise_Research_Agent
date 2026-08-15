"""Baseline smoke tests: package imports and minimal app wiring."""

from app import __version__


def test_version_is_semverish() -> None:
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_app_imports() -> None:
    from app.main import app

    assert app.title.startswith("ECRKE")
    assert app.version == __version__


def test_healthz() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
