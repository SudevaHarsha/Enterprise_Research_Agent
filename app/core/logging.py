"""Structured JSONL logging with correlation and run identifiers.

Every record is emitted as exactly one JSON object per line (JSONL) to the
configured stream. A ``correlation_id`` is always present (auto-generated when
absent) and ``run_id`` is included when the caller supplies one — either via
:func:`get_logger` context or ``extra={...}`` on a log call. Values whose keys
look like secrets are redacted before serialization; never pass raw
credentials to the logger (Ironclad Rule 01).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import MutableMapping
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import uuid4

from app.core.config import Settings, get_settings

_RESERVED_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)

_SECRET_HINTS = ("password", "passwd", "secret", "token", "credential", "authorization")


def _is_secret_key(key: str) -> bool:
    """Return True when a key name hints the value is a secret."""
    lowered = key.lower()
    if any(hint in lowered for hint in _SECRET_HINTS):
        return True
    return lowered.endswith(("_key", "_secret")) or lowered == "key"


def redact_value(key: str, value: Any) -> Any:
    """Recursively redact values whose key names hint at a secret."""
    if _is_secret_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(str(k), v) for k, v in enumerate(value)]
    return value


class JsonlFormatter(logging.Formatter):
    """Format log records as single-line JSON with correlation ids."""

    def format(self, record: logging.LogRecord) -> str:
        correlation_id = getattr(record, "correlation_id", None) or uuid4().hex
        run_id = getattr(record, "run_id", None)
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": str(correlation_id),
        }
        if run_id:
            payload["run_id"] = str(run_id)
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key in ("correlation_id", "run_id"):
                continue
            payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ContextLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Logger adapter that merges run/correlation context into each record."""

    def process(
        self, msg: str, kwargs: MutableMapping[str, Any]
    ) -> tuple[str, MutableMapping[str, Any]]:
        kwargs["extra"] = {**(self.extra or {}), **kwargs.get("extra", {})}
        return msg, kwargs


def get_logger(
    name: str,
    *,
    run_id: str | None = None,
    correlation_id: str | None = None,
) -> ContextLoggerAdapter:
    """Return a structured logger bound to optional run/correlation context."""
    return ContextLoggerAdapter(
        logging.getLogger(name),
        {"run_id": run_id, "correlation_id": correlation_id},
    )


def configure_logging(settings: Settings | None = None, stream: TextIO | None = None) -> None:
    """Install the JSONL handler on the root logger (idempotent).

    Repeated calls replace the previous handler so log output never
    duplicates lines.
    """
    effective = settings or get_settings()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonlFormatter())
    root = logging.getLogger()
    root.setLevel(effective.log_level)
    root.handlers = [handler]
