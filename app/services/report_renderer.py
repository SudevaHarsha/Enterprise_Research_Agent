"""Report rendering models and renderers (task_010, build-plan Step 10).

Pure, deterministic output layer for the report stage:

- :class:`Report` — the public report contract returned by the generator and
  consumed by the tracing / presentation layers. ``report_generator`` builds
  it; nothing in this module touches the database or the LLM gateway.
- :func:`render_markdown` — deterministic plain-markdown rendering (ASCII
  only) covering title, metadata, per-conclusion confidence / HUMAN REVIEW
  REQUIRED badge / one-sidedness / contradiction warnings / support matrix /
  evidence statements with source domain, and a references section.
- :func:`render_json` — ``model_dump(mode='json')`` so the report roundtrips
  through :class:`Report` and is JSON-serializable for API responses.

The renderers are pure functions of the report; callers decide where the
output goes (file, API response, trace artifact).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# Sentinel rendered when a conclusion has no confidence.
_UNKNOWN_CONFIDENCE = "unknown"


class ReportSupportEntry(BaseModel):
    """One row of a conclusion's support matrix (verified evidence link)."""

    statement_id: str = Field(description="Cited verified statement id.")
    passage_id: str = Field(description="Passage the statement was verified against.")
    support_score: str = Field(
        description="EvidenceScore of the latest method='verify' link: full|partial|none."
    )


class ReportEvidenceStatement(BaseModel):
    """One evidence statement cited by a conclusion, with its source domain."""

    id: str = Field(description="Cited verified statement id.")
    text: str = Field(description="Redacted statement text.")
    source_domain: str = Field(
        description="Domain of the source the statement was extracted from (from source URI)."
    )


class ReportConclusion(BaseModel):
    """One synthesized conclusion in the public report."""

    id: str = Field(description="Conclusion row id.")
    text: str = Field(description="Redacted conclusion text.")
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="LLM confidence in [0,1]; null when unjudged."
    )
    human_review_required: bool = Field(
        default=False, description="True when high-stakes content demands human review."
    )
    one_sided: bool = Field(
        default=False, description="True when the conclusion draws on <2 source domains."
    )
    contradiction_warnings: list[str] = Field(
        default_factory=list,
        description=("Summaries of confirmed contradictions citing this conclusion's statements."),
    )
    support_matrix: list[ReportSupportEntry] = Field(
        default_factory=list, description="Verified evidence scores per cited statement."
    )
    evidence_statements: list[ReportEvidenceStatement] = Field(
        default_factory=list, description="Cited statements with source domains."
    )


class Report(BaseModel):
    """The public report contract: one run's synthesized conclusions."""

    run_id: str = Field(description="Run the report was generated for.")
    topic: str = Field(description="Research topic the conclusions answer.")
    generated_at: datetime = Field(description="UTC timestamp of report generation.")
    conclusions: list[ReportConclusion] = Field(
        default_factory=list, description="Synthesized conclusions."
    )


def render_markdown(report: Report) -> str:
    """Render ``report`` as deterministic plain markdown (ASCII only).

    Sections: title + metadata, one block per conclusion (text, confidence,
    HUMAN REVIEW REQUIRED badge, one-sidedness, contradiction warnings, support
    matrix table, evidence statements with source domains), references.
    """
    lines: list[str] = []
    lines.append(f"# {report.topic}")
    lines.append("")
    lines.append(f"- Run ID: {report.run_id}")
    lines.append(f"- Generated at: {report.generated_at.isoformat()}")
    lines.append("")
    lines.append("## Conclusions")
    lines.append("")

    for index, conclusion in enumerate(report.conclusions, start=1):
        lines.append(f"### {index}. {conclusion.text}")
        lines.append("")
        if conclusion.confidence is not None:
            lines.append(f"- Confidence: {conclusion.confidence:.2f}")
        else:
            lines.append(f"- Confidence: {_UNKNOWN_CONFIDENCE}")
        if conclusion.human_review_required:
            lines.append("- **HUMAN REVIEW REQUIRED**")
        if conclusion.one_sided:
            lines.append("- One-sidedness: **one-sided**")
        else:
            lines.append("- One-sidedness: balanced")
        if conclusion.contradiction_warnings:
            lines.append("- Contradiction warnings:")
            for warning in conclusion.contradiction_warnings:
                lines.append(f"  - {warning}")
        if conclusion.support_matrix:
            lines.append("- Support matrix:")
            lines.append("  | statement_id | passage_id | support_score |")
            lines.append("  |---|---|---|")
            for entry in conclusion.support_matrix:
                lines.append(
                    f"  | {entry.statement_id} | {entry.passage_id} | {entry.support_score} |"
                )
        if conclusion.evidence_statements:
            lines.append("- Evidence statements:")
            for evidence in conclusion.evidence_statements:
                lines.append(f"  - `{evidence.id}` ({evidence.source_domain}): {evidence.text}")
        lines.append("")

    lines.append("## References")
    lines.append("")
    domains = sorted(
        {
            evidence.source_domain
            for conclusion in report.conclusions
            for evidence in conclusion.evidence_statements
            if evidence.source_domain
        }
    )
    if domains:
        for domain in domains:
            lines.append(f"- {domain}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def render_json(report: Report) -> dict[str, Any]:
    """Render ``report`` as a JSON-serializable dict (roundtrips via Report)."""
    return report.model_dump(mode="json")
