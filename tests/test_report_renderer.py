"""Unit tests for the report renderer models and markdown/JSON renderers (task_010).

Hermetic: pure Pydantic models and pure render functions — no gateway, no
database, no network. Covers: model defaults, JSON serialization + roundtrip
through ``Report``, and a complete deterministic markdown render (title,
metadata, per-conclusion confidence / HUMAN REVIEW REQUIRED badge /
one-sidedness / contradiction warnings / support matrix / evidence statements
with source domain, references section).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

from app.services.report_renderer import (
    Report,
    ReportConclusion,
    ReportEvidenceStatement,
    ReportSupportEntry,
    render_json,
    render_markdown,
)


def make_report() -> Report:
    """Build a fully-populated Report for renderer tests."""
    conclusion = ReportConclusion(
        id=str(uuid4()),
        text="Retail same-store sales grew in the latest quarter.",
        confidence=0.85,
        human_review_required=True,
        one_sided=False,
        contradiction_warnings=["Two sources disagree on the growth rate."],
        support_matrix=[
            ReportSupportEntry(
                statement_id=str(uuid4()),
                passage_id=str(uuid4()),
                support_score="full",
            )
        ],
        evidence_statements=[
            ReportEvidenceStatement(
                id=str(uuid4()),
                text="Retailers reported stronger same-store sales growth.",
                source_domain="retail.example.com",
            )
        ],
    )
    return Report(
        run_id=str(uuid4()),
        topic="Q3 retail outlook",
        generated_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
        conclusions=[conclusion],
    )


def test_report_model_defaults() -> None:
    """ReportConclusion defaults: no confidence, no flags, empty lists."""
    conclusion = ReportConclusion(id="c1", text="A conclusion.")
    assert conclusion.confidence is None
    assert conclusion.human_review_required is False
    assert conclusion.one_sided is False
    assert conclusion.contradiction_warnings == []
    assert conclusion.support_matrix == []
    assert conclusion.evidence_statements == []


def test_report_json_roundtrip() -> None:
    """render_json returns a JSON-serializable dict that roundtrips through Report."""
    report = make_report()
    payload = render_json(report)
    json.dumps(payload)  # must be serializable without raising
    assert isinstance(payload, dict)
    assert Report.model_validate(payload) == report


def test_render_markdown_complete() -> None:
    """The markdown render covers every report section deterministically."""
    report = make_report()
    markdown = render_markdown(report)

    # title + metadata
    assert markdown.startswith(f"# {report.topic}")
    assert report.run_id in markdown
    assert report.generated_at.isoformat() in markdown
    assert "## Conclusions" in markdown

    conclusion = report.conclusions[0]
    assert conclusion.text in markdown
    assert "0.85" in markdown
    assert "**HUMAN REVIEW REQUIRED**" in markdown
    assert "One-sidedness: balanced" in markdown
    assert "Two sources disagree on the growth rate." in markdown
    assert "support_score" in markdown
    assert "retail.example.com" in markdown
    assert "## References" in markdown

    # deterministic
    assert render_markdown(report) == markdown
