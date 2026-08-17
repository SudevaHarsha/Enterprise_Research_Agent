"""Relational provenance core — the audit backbone of ECRKE.

Implements the conceptual DDL in design doc §7.1 (14 tables). Write governance
(§7.2, §9.3):

- No statement enters the KB unverified (status ``draft`` until the verify-first
  gate promotes it to ``verified`` or ``quarantined``).
- ``evidence_links`` and ``audit_trace`` are append-only — enforced by ORM
  listeners (``app.db.base``) and Postgres triggers in the initial migration.
- Versioning is by new rows; no in-place mutation of beliefs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDMixin, register_append_only
from app.db.enums import (
    ContradictionStatus,
    EvidenceScore,
    EvidenceTier,
    RunStatus,
    SourceStatus,
    SourceType,
    StatementStatus,
)


class Tenant(UUIDMixin, TimestampMixin, Base):
    """Tenant isolation root. All research data hangs off a tenant."""

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    rbac_policy: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    runs: Mapped[list[Run]] = relationship(back_populates="tenant")


class Run(UUIDMixin, Base):
    """One research run, one submitted question, one observable lifecycle."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted','planning','searching','collecting','storing',"
            "'extracting','comparing','verifying','detecting','concluding','tracing',"
            "'completed','failed','paused','cancelled')",
            name="valid_run_status",
        ),
        CheckConstraint("progress >= 0.0 AND progress <= 1.0", name="valid_progress"),
        CheckConstraint("cost_spent_usd >= 0.0", name="non_negative_cost"),
        Index("ix_runs_tenant_status", "tenant_id", "status"),
        Index("ix_runs_tenant_created", "tenant_id", "created_at"),
    )

    tenant_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RunStatus.SUBMITTED.value
    )
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_budget_usd: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=0.0)
    cost_spent_usd: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False, default=0.0)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="runs")
    sources: Mapped[list[Source]] = relationship(back_populates="run")
    checkpoints: Mapped[list[Checkpoint]] = relationship(back_populates="run")


class Source(UUIDMixin, Base):
    """Anything fetched: web page, PDF, RSS item, uploaded document."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("run_id", "content_hash", name="uq_sources_run_content_hash"),
        CheckConstraint(
            "source_type IN ('web','pdf','rss','docx','rtf','upload','other')",
            name="valid_source_type",
        ),
        CheckConstraint(
            "status IN ('pending','fetched','failed','normalized','quarantined')",
            name="valid_source_status",
        ),
        Index("ix_sources_run_id", "run_id"),
        Index("ix_sources_uri", "uri"),
    )

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    uri: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SourceType.WEB.value
    )
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    allowlisted_uri: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SourceStatus.PENDING.value
    )

    run: Mapped[Run] = relationship(back_populates="sources")
    passages: Mapped[list[Passage]] = relationship(back_populates="source")


class Passage(UUIDMixin, Base):
    """Atomic retrievable unit of a source."""

    __tablename__ = "passages"
    __table_args__ = (
        UniqueConstraint("source_id", "seq", name="uq_passages_source_seq"),
        Index("ix_passages_source_id", "source_id"),
        Index("ix_passages_hash", "hash"),
    )

    source_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    start_char: Mapped[int | None] = mapped_column(nullable=True)
    end_char: Mapped[int | None] = mapped_column(nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)

    source: Mapped[Source] = relationship(back_populates="passages")
    statements: Mapped[list[Statement]] = relationship(back_populates="passage")


class Statement(UUIDMixin, Base):
    """Atomic claim extracted from a passage. Draft until verify-first gate."""

    __tablename__ = "statements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','verified','quarantined')", name="valid_statement_status"
        ),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="valid_confidence",
        ),
        Index("ix_statements_passage_id", "passage_id"),
        Index("ix_statements_run_id", "run_id"),
        Index("ix_statements_status", "status"),
        Index("ix_statements_run_status", "run_id", "status"),
    )

    passage_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("passages.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StatementStatus.DRAFT.value
    )
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    passage: Mapped[Passage] = relationship(back_populates="statements")


class EvidenceLink(UUIDMixin, Base):
    """Statement → passage resolution — the audit backbone. APPEND-ONLY."""

    __tablename__ = "evidence_links"
    __table_args__ = (
        CheckConstraint("score IN ('full','partial','none')", name="valid_evidence_score"),
        Index("ix_evidence_links_statement_id", "statement_id"),
        Index("ix_evidence_links_passage_id", "passage_id"),
        Index("ix_evidence_links_run_id", "run_id"),
    )

    statement_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("statements.id", ondelete="RESTRICT"), nullable=False
    )
    passage_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("passages.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    score: Mapped[str] = mapped_column(String(16), nullable=False, default=EvidenceScore.NONE.value)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Finding(UUIDMixin, Base):
    """Grouped/classified statements."""

    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint("evidence_tier IN ('t1','t2','t3','t4')", name="valid_evidence_tier"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="valid_confidence",
        ),
        Index("ix_findings_run_id", "run_id"),
    )

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    evidence_tier: Mapped[str | None] = mapped_column(
        String(16), nullable=True, default=EvidenceTier.T3.value
    )
    domain_tags: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    stance: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FindingStatement(Base):
    """M2M: findings ← statements."""

    __tablename__ = "finding_statements"
    __table_args__ = (Index("ix_finding_statements_statement_id", "statement_id"),)

    finding_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("findings.id", ondelete="CASCADE"), primary_key=True
    )
    statement_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("statements.id", ondelete="RESTRICT"), primary_key=True
    )


class Contradiction(UUIDMixin, Base):
    """Flagged + confirmed conflicts between verified statements."""

    __tablename__ = "contradictions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('flagged','confirmed','rejected')", name="valid_contradiction_status"
        ),
        Index("ix_contradictions_run_id", "run_id"),
        Index("ix_contradictions_stmt_a", "statement_a_id"),
        Index("ix_contradictions_stmt_b", "statement_b_id"),
        Index("ix_contradictions_status", "status"),
    )

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    statement_a_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("statements.id", ondelete="RESTRICT"), nullable=False
    )
    statement_b_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("statements.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ContradictionStatus.FLAGGED.value
    )
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Conclusion(UUIDMixin, Base):
    """Final output. Every conclusion links to evidence (never naked)."""

    __tablename__ = "conclusions"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)",
            name="valid_confidence",
        ),
        Index("ix_conclusions_run_id", "run_id"),
    )

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_review_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ConclusionEvidence(Base):
    """M2M: conclusions ← statements (+ optional finding)."""

    __tablename__ = "conclusion_evidence"
    __table_args__ = (Index("ix_conclusion_evidence_statement_id", "statement_id"),)

    conclusion_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("conclusions.id", ondelete="CASCADE"), primary_key=True
    )
    statement_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("statements.id", ondelete="RESTRICT"), primary_key=True
    )
    finding_id: Mapped[Any | None] = mapped_column(
        Uuid, ForeignKey("findings.id", ondelete="RESTRICT"), nullable=True
    )


class AuditTrace(UUIDMixin, Base):
    """Immutable log of every KB write decision. APPEND-ONLY."""

    __tablename__ = "audit_trace"
    __table_args__ = (
        Index("ix_audit_trace_run_id", "run_id"),
        Index("ix_audit_trace_entity", "entity_type", "entity_id"),
        Index("ix_audit_trace_ts", "ts"),
    )

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Checkpoint(UUIDMixin, Base):
    """Durable resume points for long runs — one row per (run, stage)."""

    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("run_id", "stage", name="uq_checkpoints_run_stage"),)

    run_id: Mapped[Any] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run: Mapped[Run] = relationship(back_populates="checkpoints")


class KVEntry(Base):
    """kv_cache — repeat-call cache (LLM answers, rate counters). Replaces Redis."""

    __tablename__ = "kv_cache"
    __table_args__ = (Index("ix_kv_cache_expires_at", "expires_at"),)

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


register_append_only(EvidenceLink)
register_append_only(AuditTrace)
