"""Unit tests for ``app.core.telemetry``.

Covers the task_003 stub contract: a working tracer is always available and
no OTLP endpoint is required. Real exporters/metrics land with task_013.
"""

from __future__ import annotations

from app.core.config import Settings
from app.core.telemetry import get_tracer, get_tracer_provider, setup_telemetry


def test_tracer_works_without_otlp_endpoint() -> None:
    """A tracer is returned and records spans with no OTLP endpoint configured."""
    setup_telemetry(Settings(otel_exporter_otlp_endpoint=""))
    tracer = get_tracer("test.telemetry")
    assert tracer is not None
    with tracer.start_as_current_span("span.one") as span:
        span.set_attribute("test.key", "value")
        assert span.is_recording()


def test_provider_defaults_without_endpoint() -> None:
    """The provider is available and its spans are recording (in-memory stub)."""
    setup_telemetry(Settings(otel_exporter_otlp_endpoint=""))
    provider = get_tracer_provider()
    assert provider is not None
    tracer = provider.get_tracer("test.provider")
    span = tracer.start_span("span.two")
    try:
        assert span.is_recording()
    finally:
        span.end()


def test_get_tracer_defaults_to_app_provider() -> None:
    """``get_tracer()`` returns the application tracer without configuration."""
    setup_telemetry(Settings(otel_exporter_otlp_endpoint=""))
    tracer = get_tracer()
    assert tracer is not None
    span = tracer.start_span("span.three")
    try:
        assert span.is_recording()
    finally:
        span.end()
