"""OpenTelemetry scaffolding (stub — task_013 wires exporters/metrics).

The application always has a working :class:`~opentelemetry.trace.Tracer`.
When no OTLP endpoint is configured (the default) spans are recorded in memory
and never leave the process, so tracing works with zero infrastructure.
Setting ``OTEL_EXPORTER_OTLP_ENDPOINT`` switches the provider to a batch OTLP
exporter; task_013 layers Prometheus metrics and FastAPI instrumentation on
top of this provider.
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings, get_settings

_provider: TracerProvider | None = None


def _build_provider(settings: Settings) -> TracerProvider:
    resource = Resource.create({SERVICE_NAME: settings.otel_service_name})
    provider = TracerProvider(resource=resource)
    endpoint = settings.otel_exporter_otlp_endpoint.strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    else:
        # Stub: record spans in memory; nothing is exported (task_013).
        provider.add_span_processor(SimpleSpanProcessor(InMemorySpanExporter()))
    return provider


def setup_telemetry(settings: Settings | None = None) -> TracerProvider:
    """(Re)configure the global tracer provider and register it."""
    global _provider
    effective = settings or get_settings()
    _provider = _build_provider(effective)
    trace.set_tracer_provider(_provider)
    return _provider


def get_tracer_provider() -> TracerProvider:
    """Return the configured provider, initializing it on first use."""
    if _provider is None:
        return setup_telemetry()
    return _provider


def get_tracer(name: str = "ecrke") -> trace.Tracer:
    """Return a working tracer; safe with no OTLP endpoint configured."""
    return get_tracer_provider().get_tracer(name)
