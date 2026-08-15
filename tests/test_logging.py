"""Unit tests for ``app.core.logging``.

Covers: valid JSONL output, correlation_id/run_id propagation, auto-generated
defaults when ids are absent, structured extra fields, and secret redaction
(Ironclad Rule 01).
"""

from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.core.logging import configure_logging, get_logger


def _last_line(out: str) -> dict[str, object]:
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines, "expected at least one log line"
    return json.loads(lines[-1])


def test_emits_valid_jsonl_with_correlation_and_run_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Structured logging emits JSONL lines carrying both ids when provided."""
    configure_logging(Settings(log_level="INFO"))
    logger = get_logger("test.app", run_id="run-123", correlation_id="corr-456")
    logger.info("hello %s", "world")

    record = _last_line(capsys.readouterr().out)
    assert record["correlation_id"] == "corr-456"
    assert record["run_id"] == "run-123"
    assert record["message"] == "hello world"
    assert record["level"] == "INFO"
    assert "ts" in record
    assert "logger" in record


def test_defaults_generated_when_ids_absent(capsys: pytest.CaptureFixture[str]) -> None:
    """Logger works when run_id/correlation_id are absent (defaults, no crash)."""
    configure_logging(Settings(log_level="INFO"))
    logger = get_logger("test.app")
    logger.info("no ids supplied")

    record = _last_line(capsys.readouterr().out)
    assert record["correlation_id"], "correlation_id should be auto-generated"
    assert isinstance(record["correlation_id"], str)


def test_structured_extra_fields_included(capsys: pytest.CaptureFixture[str]) -> None:
    """Structured ``extra`` kwargs are merged into the JSON payload."""
    configure_logging(Settings(log_level="INFO"))
    logger = get_logger("test.app", run_id="run-9")
    logger.info("event %s", "collect", extra={"stage": "stage_search", "docs": 3})

    record = _last_line(capsys.readouterr().out)
    assert record["stage"] == "stage_search"
    assert record["docs"] == 3
    assert record["run_id"] == "run-9"
    assert "correlation_id" in record


def test_secret_values_redacted_in_extra(capsys: pytest.CaptureFixture[str]) -> None:
    """Secret-looking keys are redacted; plaintext never hits the stream."""
    configure_logging(Settings(log_level="INFO"))
    logger = get_logger("test.app")
    logger.info("with secret", extra={"api_key": "sk-live-abc123", "body": "ok"})

    out = capsys.readouterr().out
    record = _last_line(out)
    assert record["api_key"] == "[REDACTED]"
    assert "sk-live-abc123" not in out
    assert record["body"] == "ok"
