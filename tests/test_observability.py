"""Observability integration tests (task_013) — hermetic end-to-end wiring.

Covers: ``GET /metrics`` Prometheus exposition over HTTP, flow lifecycle
events + metric recording through the in-process ``prefect_harness`` (full /
paused / failed paths), the ``DefaultPipelineRunner`` Prefect submission mode
(monkeypatched client — no real Prefect server), the Grafana dashboard JSON,
and the docker-compose observability profile. No real Prefect server,
Prometheus, Grafana, LLM, database, or network is touched.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import test_pipeline_flows as flows_harness
import yaml
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from prometheus_client.parser import text_string_to_metric_families

from app.core import metrics
from app.core.config import Settings
from app.main import app
from app.pipeline.context import STAGES

REPO_ROOT = Path(__file__).resolve().parent.parent


def _flow_events(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str | None]]:
    """(event, stage) pairs emitted by pipeline lifecycle events."""
    return [
        (getattr(record, "event", None), getattr(record, "stage", None))
        for record in caplog.records
        if getattr(record, "event", None) is not None
    ]


# --------------------------------------------------------------------------- #
# GET /metrics (brief test 3)
# --------------------------------------------------------------------------- #
def test_metrics_endpoint_returns_prometheus_text() -> None:
    metrics.reset_registry()
    metrics.record_verification(True)
    metrics.record_run_status("completed")
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(CONTENT_TYPE_LATEST.split(";")[0])
    text = response.text
    for name in (
        "ecrke_run_cost_spent_usd",
        "ecrke_stage_duration_seconds",
        "ecrke_verification_pass_total",
        "ecrke_verification_fail_total",
        "ecrke_verification_pass_rate",
        "ecrke_contradictions_confirmed_total",
        "ecrke_kb_sources_total",
        "ecrke_kb_passages_total",
        "ecrke_kb_statements_total",
        "ecrke_runs_total",
    ):
        assert name in text
    # valid Prometheus exposition: the body parses without error
    list(text_string_to_metric_families(text))


def test_metrics_router_mounted_on_app() -> None:
    assert "/metrics" in app.openapi()["paths"]


# --------------------------------------------------------------------------- #
# Flow lifecycle events through prefect_harness (brief tests 5-6)
# --------------------------------------------------------------------------- #
async def test_full_run_emits_stage_events_and_run_completed(
    prefect_harness: object, caplog: pytest.LogCaptureFixture
) -> None:
    harness = flows_harness.FlowHarness()
    from app.pipeline.flows import research_pipeline

    with caplog.at_level(logging.INFO):
        result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    events = _flow_events(caplog)
    assert events.count(("run_completed", None)) == 1
    for stage in STAGES:
        assert events.count(("stage_started", stage)) == 1
        assert events.count(("stage_completed", stage)) == 1


async def test_full_run_records_stage_durations_and_cost(
    prefect_harness: object,
) -> None:
    metrics.reset_registry()
    harness = flows_harness.FlowHarness()
    from app.pipeline.flows import research_pipeline

    result = await research_pipeline(harness.run.id, harness.services)
    assert result == "completed"
    registry = metrics.get_registry()
    for stage in STAGES:
        assert (
            registry.get_sample_value("ecrke_stage_duration_seconds_count", {"stage": stage}) == 1
        )
    # run cost gauge keyed by run_id was recorded after the run finished
    assert (
        registry.get_sample_value("ecrke_run_cost_spent_usd", {"run_id": str(harness.run.id)})
        is not None
    )


async def test_budget_breach_emits_run_paused_with_reason(
    prefect_harness: object, caplog: pytest.LogCaptureFixture
) -> None:
    harness = flows_harness.FlowHarness(budget="5.00")
    harness.run.cost_spent_usd = Decimal("0.60")  # over the stage budget
    from app.pipeline.flows import research_pipeline

    with caplog.at_level(logging.INFO):
        result = await research_pipeline(harness.run.id, harness.services)
    assert result == "paused"
    events = _flow_events(caplog)
    assert ("run_paused", None) in events
    paused = [r for r in caplog.records if getattr(r, "event", None) == "run_paused"]
    assert paused
    assert "budget" in str(getattr(paused[0], "reason", ""))
    assert ("run_completed", None) not in events  # never fabricates success


async def test_failed_run_emits_run_failed_not_completed(
    prefect_harness: object, caplog: pytest.LogCaptureFixture
) -> None:
    harness = flows_harness.FlowHarness()
    harness.verifier.fail_after = 1  # verify (stage 6) dies mid-run
    from app.pipeline.flows import research_pipeline

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError, match="simulated kill"):
        await research_pipeline(harness.run.id, harness.services)
    events = _flow_events(caplog)
    assert ("run_failed", None) in events
    failed = [r for r in caplog.records if getattr(r, "event", None) == "run_failed"]
    assert failed
    assert "simulated kill" in str(getattr(failed[0], "error", ""))
    assert ("run_completed", None) not in events  # Rule 04: no fabricated success


# --------------------------------------------------------------------------- #
# DefaultPipelineRunner submission mode (brief test 8)
# --------------------------------------------------------------------------- #
class FakePrefectClient:
    """In-memory PrefectClient stand-in recording every submission call."""

    def __init__(self, api_url: str) -> None:
        self.api_url = api_url
        self.calls: list[tuple[str, object]] = []
        self.deployment_id = uuid4()

    async def __aenter__(self) -> FakePrefectClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def read_deployment_by_name(self, name: str) -> SimpleNamespace:
        self.calls.append(("read_deployment_by_name", name))
        assert name == "research-pipeline/research-worker"
        return SimpleNamespace(id=self.deployment_id)

    async def create_flow_run_from_deployment(
        self, deployment_id: UUID, *, parameters: dict[str, object] | None = None, **_: object
    ) -> SimpleNamespace:
        self.calls.append(("create_flow_run_from_deployment", deployment_id))
        return SimpleNamespace(
            id=uuid4(),
            state=SimpleNamespace(name="Scheduled"),
        )


async def test_runner_default_in_process_mode_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import deps

    called: list[object] = []

    async def fake_flow(run_id: object, services: object) -> str:
        called.append(run_id)
        return "completed"

    monkeypatch.setattr(deps, "research_pipeline", fake_flow)
    runner = deps.DefaultPipelineRunner(
        session_factory=lambda: None,  # type: ignore[arg-type]
        settings=Settings(app_env="test", prefect_api_url=""),
        build_services=lambda settings, session_factory: None,  # type: ignore[arg-type]
    )
    result = await runner.run("run-1")
    assert result == "completed"
    assert called == ["run-1"]


async def test_runner_submits_via_prefect_client_when_api_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import deps

    created: list[FakePrefectClient] = []

    def fake_factory(api_url: str) -> FakePrefectClient:
        client = FakePrefectClient(api_url)
        created.append(client)
        return client

    monkeypatch.setattr(deps, "_make_prefect_client", fake_factory)
    runner = deps.DefaultPipelineRunner(
        session_factory=lambda: None,  # type: ignore[arg-type]
        settings=Settings(app_env="test", prefect_api_url="http://prefect.internal:4200/api"),
        build_services=lambda settings, session_factory: None,  # type: ignore[arg-type]
    )
    run_id = uuid4()
    result = await runner.run(run_id)
    assert result == "Scheduled"
    assert len(created) == 1
    client = created[0]
    create_calls = [c for c in client.calls if c[0] == "create_flow_run_from_deployment"]
    assert create_calls
    # submission went through the prefect client (not in-process)
    assert client.deployment_id == create_calls[0][1]
    read_calls = [c for c in client.calls if c[0] == "read_deployment_by_name"]
    assert read_calls == [("read_deployment_by_name", "research-pipeline/research-worker")]


async def test_runner_prefect_mode_passes_run_id_as_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import deps

    captured: dict[str, object] = {}

    class ParamClient(FakePrefectClient):
        async def create_flow_run_from_deployment(
            self, deployment_id: UUID, *, parameters: dict[str, object] | None = None, **_: object
        ) -> SimpleNamespace:
            captured["parameters"] = parameters
            return SimpleNamespace(id=uuid4(), state=SimpleNamespace(name="Scheduled"))

    monkeypatch.setattr(deps, "_make_prefect_client", lambda api_url: ParamClient(api_url))
    runner = deps.DefaultPipelineRunner(
        session_factory=lambda: None,  # type: ignore[arg-type]
        settings=Settings(app_env="test", prefect_api_url="http://prefect.internal:4200/api"),
    )
    run_id = uuid4()
    await runner.resume(run_id)
    assert captured.get("parameters") == {"run_id": str(run_id)}


# --------------------------------------------------------------------------- #
# Grafana dashboard (brief test 9)
# --------------------------------------------------------------------------- #
def test_grafana_dashboard_valid_json_with_six_panels() -> None:
    dashboard = json.loads((REPO_ROOT / "grafana" / "dashboard.json").read_text(encoding="utf-8"))
    panels = dashboard["panels"]
    assert len(panels) >= 6
    titles = [panel["title"] for panel in panels]
    for expected in (
        "Cost by model tier",
        "Pipeline health",
        "Stage durations",
        "Verification pass rate",
        "Contradiction counts",
        "KB growth",
    ):
        assert expected in titles
    expressions = [target["expr"] for panel in panels for target in panel["targets"]]
    assert expressions
    for expr in expressions:
        assert "ecrke_" in expr


# --------------------------------------------------------------------------- #
# docker-compose (brief test 10)
# --------------------------------------------------------------------------- #
def test_compose_default_services_unchanged_and_observability_profile() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("postgres", "prefect-server", "api", "worker"):
        assert name in services, f"default service {name!r} missing"
    # default services are not profile-gated (default `docker compose up` unchanged)
    for name in ("postgres", "prefect-server", "api", "worker"):
        assert "profiles" not in services[name]

    prometheus = services["prometheus"]
    assert prometheus["image"] == "prom/prometheus"
    assert "observability" in prometheus["profiles"]

    grafana = services["grafana"]
    assert grafana["image"] == "grafana/grafana"
    assert "observability" in grafana["profiles"]


def test_prometheus_scrape_config_targets_api_metrics() -> None:
    scrape = yaml.safe_load(
        (REPO_ROOT / "prometheus" / "prometheus.yml").read_text(encoding="utf-8")
    )
    jobs = scrape["scrape_configs"]
    assert jobs
    assert any("api:8000" in job.get("static_configs", [{}])[0].get("targets", []) for job in jobs)
    assert all(job.get("metrics_path") == "/metrics" for job in jobs)


def test_grafana_dashboard_provisioned() -> None:
    provider = yaml.safe_load(
        (REPO_ROOT / "grafana" / "provisioning" / "dashboards" / "dashboard.yml").read_text(
            encoding="utf-8"
        )
    )
    assert provider["providers"][0]["type"] == "file"
    datasource = yaml.safe_load(
        (REPO_ROOT / "grafana" / "provisioning" / "datasources" / "datasource.yml").read_text(
            encoding="utf-8"
        )
    )
    assert datasource["datasources"][0]["type"] == "prometheus"
    # dashboard panels reference the provisioned datasource uid
    dashboard = json.loads((REPO_ROOT / "grafana" / "dashboard.json").read_text(encoding="utf-8"))
    uids = {panel.get("datasource", {}).get("uid") for panel in dashboard["panels"]}
    assert "prometheus" in uids
