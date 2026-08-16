"""Structured run lifecycle events (task_013, design doc §14).

Each lifecycle transition emits exactly one JSONL line through the app's
structured logger (``app.core.logging``). Every line carries:

- ``run_id`` — the run this event belongs to
- ``correlation_id`` — ``run_id`` by default (per-run correlation, design §14)
- ``event`` — the event name (``stage_started`` | ``stage_completed`` |
  ``run_completed`` | ``run_paused`` | ``run_failed``)
- ``ts`` — ISO-8601 timestamp added by the JSONL formatter

G-05 / Rule 01: every detail field is redacted with ``redact_json`` BEFORE it
reaches the logger, so secret-looking substrings never appear in the emitted
line. Callers still never pass raw credentials (the logger's own key-based
redaction is a second line of defense, not a substitute).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.logging import get_logger
from app.services.audit_writer import redact_json

_EVENT_LOGGER_NAME = "app.pipeline.events"


def _emit(run_id: UUID | str, event: str, **fields: Any) -> None:
    """Emit one event line: run-scoped logger + redacted extra fields."""
    run_key = str(run_id)
    logger = get_logger(_EVENT_LOGGER_NAME, run_id=run_key, correlation_id=run_key)
    extra: dict[str, Any] = {"event": event}
    for key, value in fields.items():
        extra[key] = redact_json(value)
    logger.info(event, extra=extra)


def emit_stage_started(
    run_id: UUID | str,
    stage: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit ``stage_started`` when a stage begins executing."""
    _emit(run_id, "stage_started", stage=stage, detail=detail)


def emit_stage_completed(
    run_id: UUID | str,
    stage: str,
    ok: bool,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit ``stage_completed`` after a stage checkpoint is durable."""
    _emit(run_id, "stage_completed", stage=stage, ok=ok, detail=detail)


def emit_run_completed(
    run_id: UUID | str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit ``run_completed`` when every stage finishes successfully."""
    _emit(run_id, "run_completed", detail=detail)


def emit_run_paused(
    run_id: UUID | str,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit ``run_paused`` when a cost breach (G-03) halts the run."""
    _emit(run_id, "run_paused", reason=reason, detail=detail)


def emit_run_failed(
    run_id: UUID | str,
    error: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Emit ``run_failed`` on any unhandled failure (never fabricates success)."""
    _emit(run_id, "run_failed", error=error, detail=detail)


__all__ = [
    "emit_stage_started",
    "emit_stage_completed",
    "emit_run_completed",
    "emit_run_paused",
    "emit_run_failed",
]
