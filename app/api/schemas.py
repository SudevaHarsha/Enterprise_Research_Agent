"""Pydantic request/response models for the evaluator API (task_012).

Contract models for the ten ``/v1`` endpoints (design doc §10). Validation
lives at the request boundary: malformed input is rejected with 422. Response
models are the JSON contract the evaluator consumes; all text exposed through
them is redacted upstream (G-05) before a model is constructed, so secret-
looking substrings never reach a response body (Ironclad Rule 01).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RunCreate(BaseModel):
    """Submit a research question; optionally execute it synchronously."""

    question: str = Field(
        min_length=1,
        max_length=2000,
        description="Research question (1..2000 characters, non-empty).",
    )
    cost_budget_usd: Decimal | None = Field(
        default=None,
        ge=0,
        description="Per-run budget in USD; defaults to the configured run budget.",
    )
    execute: bool = Field(
        default=True,
        description="Run the pipeline synchronously after creating the run.",
    )

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        """Reject whitespace-only questions and normalize the stored value."""
        stripped = value.strip()
        if not stripped:
            raise ValueError("question must be non-empty")
        return stripped


class RunRead(BaseModel):
    """Observable lifecycle of one run (poll target for the evaluator)."""

    run_id: UUID = Field(description="Run row id.")
    tenant_id: UUID = Field(description="Owning tenant id.")
    question: str = Field(description="Redacted research question.")
    status: str = Field(description="RunStatus value (submitted..completed/failed/paused).")
    stage: str | None = Field(default=None, description="Current pipeline stage, if any.")
    progress: float = Field(default=0.0, ge=0.0, le=1.0, description="Progress in [0,1].")
    cost_budget_usd: float | None = Field(default=None, description="Per-run budget in USD.")
    cost_spent_usd: float = Field(default=0.0, description="Metered spend in USD (JSON number).")
    created_at: datetime | None = Field(default=None, description="Run creation timestamp.")
    completed_at: datetime | None = Field(default=None, description="Completion timestamp.")


class StageInfo(BaseModel):
    """One durable pipeline checkpoint exposed as an observable stage."""

    stage: str = Field(description="Canonical stage name (define..trace).")
    ts: datetime = Field(description="Checkpoint timestamp.")
    summary: dict[str, Any] | None = Field(default=None, description="Redacted stage result.")


class EvidenceRef(BaseModel):
    """One evidence link on a conclusion (statement-level provenance)."""

    statement_id: UUID = Field(description="Cited verified statement id.")
    finding_id: UUID | None = Field(
        default=None, description="Optional finding the statement groups into."
    )


class ConclusionRead(BaseModel):
    """One final conclusion with its evidence links."""

    id: UUID = Field(description="Conclusion row id.")
    text: str = Field(description="Redacted conclusion text.")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    human_review_required: bool = Field(default=False)
    evidence: list[EvidenceRef] = Field(default_factory=list, description="Cited statements.")


class TraceNode(BaseModel):
    """One node of the provenance chain."""

    id: UUID = Field(description="Row id of the node.")
    kind: str = Field(description="One of: statement | passage | source.")
    text: str | None = Field(default=None, description="Redacted text (statement/passage).")
    uri: str | None = Field(default=None, description="Source URI (source node).")
    title: str | None = Field(default=None, description="Source title (source node).")


class TraceChain(BaseModel):
    """Full provenance chain: statement -> passage -> source (exactly 3 nodes)."""

    statement: TraceNode
    passage: TraceNode
    source: TraceNode


class ContradictionRead(BaseModel):
    """One flagged/confirmed contradiction between two statements."""

    id: UUID
    statement_a_id: UUID
    statement_b_id: UUID
    status: str = Field(description="flagged | confirmed | rejected.")
    evidence: dict[str, Any] | None = Field(default=None, description="Redacted evidence.")
    created_at: datetime | None = Field(default=None)


class ReportRead(BaseModel):
    """Rendered run report (markdown)."""

    run_id: UUID
    markdown: str = Field(description="Deterministic markdown rendering (no LLM in the API).")


class KBSearchResult(BaseModel):
    """One verified statement from the reusable, tenant-scoped KB."""

    statement_id: UUID
    run_id: UUID
    text: str = Field(description="Redacted statement text.")
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    passage_text: str | None = Field(default=None, description="Redacted source passage snippet.")
    source_uri: str | None = Field(
        default=None, description="Source URI the statement was verified against."
    )


class AuditRow(BaseModel):
    """One immutable audit_trace row (redacted)."""

    id: UUID
    ts: datetime | None = Field(default=None)
    entity_type: str
    entity_id: str | None = Field(default=None)
    action: str
    actor: str | None = Field(default=None)
    decision: str | None = Field(default=None)
    reason: str | None = Field(default=None)
    evidence: dict[str, Any] | None = Field(default=None, description="Redacted evidence JSON.")


class AuditExport(BaseModel):
    """Full redacted audit trace for one run."""

    run_id: UUID
    count: int = Field(ge=0)
    rows: list[AuditRow] = Field(default_factory=list)


class ErrorDetail(BaseModel):
    """RFC 7807 Problem Details for HTTP API error responses."""

    type: str = Field(default="about:blank", description="URI identifying the problem type.")
    title: str = Field(description="Human-readable summary.")
    status: int = Field(ge=100, le=599, description="HTTP status code.")
    detail: str = Field(description="Specific occurrence detail (redacted).")
    instance: str | None = Field(default=None, description="URI of the specific occurrence.")
