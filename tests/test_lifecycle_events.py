"""Unit tests for ``app.pipeline.events`` (task_013 — lifecycle events).

Every lifecycle event is emitted as exactly one structured JSONL line through
the app's structured logger, carrying ``run_id`` + ``correlation_id`` (equal
for run-scoped events) + ``event`` + ``ts``. All detail fields are redacted
with ``redact_json`` (G-05) so a secret-looking substring in a detail value
never reaches the emitted line (Ironclad Rule 01).
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from app.core.config import Settings
from app.core.logging import JsonlFormatter, configure_logging
from app.pipeline.events import (
    emit_run_completed,
    emit_run_failed,
    emit_run_paused,
    emit_stage_completed,
    emit_stage_started,
)

SECRET = "sk-fake-test-1234567890"  # noqa: S105 - fake fixture value; must be redacted


@pytest.fixture
def jsonl_capture() -> io.StringIO:
    """Replace root handlers with the JSONL stream; restore afterwards."""
    stream = io.StringIO()
    root = logging.getLogger()
    previous_handlers = root.handlers[:]
    previous_level = root.level
    configure_logging(Settings(app_env="test", log_level="INFO"), stream=stream)
    try:
        yield stream
    finally:
        root.handlers = previous_handlers
        root.setLevel(previous_level)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    """Parse every JSONL line emitted into the capture stream."""
    content = stream.getvalue().strip()
    if not content:
        return []
    return [json.loads(line) for line in content.splitlines()]


def test_stage_started_emits_one_structured_line(jsonl_capture: io.StringIO) -> None:
    emit_stage_started("run-1", "define")
    lines = _lines(jsonl_capture)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "stage_started"
    assert line["run_id"] == "run-1"
    assert line["correlation_id"] == "run-1"
    assert line["stage"] == "define"
    assert "ts" in line


def test_stage_completed_emits_ok_flag_and_stage(jsonl_capture: io.StringIO) -> None:
    emit_stage_completed("run-1", "search", ok=True)
    line = _lines(jsonl_capture)[0]
    assert line["event"] == "stage_completed"
    assert line["stage"] == "search"
    assert line["ok"] is True


def test_terminal_events_emit_reason_and_error(jsonl_capture: io.StringIO) -> None:
    emit_run_completed("run-1")
    emit_run_paused("run-1", reason="budget breached")
    emit_run_failed("run-1", error="boom")
    lines = _lines(jsonl_capture)
    assert [line["event"] for line in lines] == [
        "run_completed",
        "run_paused",
        "run_failed",
    ]
    assert lines[1]["reason"] == "budget breached"
    assert lines[2]["error"] == "boom"
    for line in lines:
        assert line["run_id"] == "run-1"
        assert line["correlation_id"] == "run-1"
        assert "ts" in line


def test_detail_redacted_with_g05(jsonl_capture: io.StringIO) -> None:
    emit_stage_completed(
        "run-1",
        "collect",
        ok=True,
        detail={"query": f"search with {SECRET}"},
    )
    line = _lines(jsonl_capture)[0]
    rendered = json.dumps(line)
    assert SECRET not in rendered
    assert "REDACTED" in line["detail"]["query"]


def test_events_flow_through_standard_logging(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_run_completed("run-9")
    records = [r for r in caplog.records if getattr(r, "event", None) == "run_completed"]
    assert records
    assert str(getattr(records[0], "run_id", "")) == "run-9"
    assert str(getattr(records[0], "correlation_id", "")) == "run-9"


def test_event_record_renders_via_jsonl_formatter() -> None:
    """The emitted LogRecord serializes to one JSON line with all fields."""
    logger = logging.getLogger("app.pipeline.events.test")
    record = logger.makeRecord(
        name="app.pipeline.events",
        level=logging.INFO,
        fn=__file__,
        lno=1,
        msg="stage_started",
        args=(),
        exc_info=None,
        extra={
            "run_id": "run-7",
            "correlation_id": "run-7",
            "event": "stage_started",
            "stage": "define",
        },
    )
    rendered = JsonlFormatter().format(record)
    parsed = json.loads(rendered)
    assert parsed["event"] == "stage_started"
    assert parsed["run_id"] == "run-7"
    assert parsed["correlation_id"] == "run-7"
    assert parsed["stage"] == "define"
