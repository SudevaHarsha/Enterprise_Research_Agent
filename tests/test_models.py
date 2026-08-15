"""Unit tests for the provenance core that need no database.

Covers: enum values, table inventory on metadata, FK/index definitions, and
the append-only ORM guard wiring.
"""

import pytest
from sqlalchemy import event

from app.db import models
from app.db.base import APPEND_ONLY_MODELS, AppendOnlyViolation, Base, _block_update_or_delete
from app.db.enums import (
    ContradictionStatus,
    EvidenceScore,
    RunStatus,
    SourceStatus,
    SourceType,
    StatementStatus,
)

EXPECTED_TABLES = {
    "tenants",
    "runs",
    "sources",
    "passages",
    "statements",
    "evidence_links",
    "findings",
    "finding_statements",
    "contradictions",
    "conclusions",
    "conclusion_evidence",
    "audit_trace",
    "checkpoints",
    "kv_cache",
}

APPEND_ONLY = {"evidence_links", "audit_trace"}


def test_all_14_tables_registered() -> None:
    actual = set(Base.metadata.tables.keys())
    assert actual >= EXPECTED_TABLES, f"missing tables: {EXPECTED_TABLES - actual}"


def test_append_only_models_registered() -> None:
    names = {m.__tablename__ for m in APPEND_ONLY_MODELS}
    assert names == APPEND_ONLY


def test_append_only_guard_raises() -> None:
    from types import SimpleNamespace

    fake_mapper = SimpleNamespace(class_=models.EvidenceLink)
    with pytest.raises(AppendOnlyViolation):
        _block_update_or_delete(fake_mapper, None, models.EvidenceLink())  # type: ignore[arg-type]


def test_append_only_listeners_wired() -> None:
    for model in (models.EvidenceLink, models.AuditTrace):
        assert event.contains(model, "before_update", _block_update_or_delete)
        assert event.contains(model, "before_delete", _block_update_or_delete)


def test_enums() -> None:
    assert RunStatus.SUBMITTED.value == "submitted"
    assert StatementStatus.DRAFT.value == "draft"
    assert StatementStatus.VERIFIED.value == "verified"
    assert StatementStatus.QUARANTINED.value == "quarantined"
    assert EvidenceScore.FULL.value == "full"
    assert ContradictionStatus.CONFIRMED.value == "confirmed"
    assert SourceStatus.NORMALIZED.value == "normalized"
    assert SourceType.PDF.value == "pdf"


def test_key_indexes_defined() -> None:
    tables = Base.metadata.tables
    idx_names = {i.name for i in tables["evidence_links"].indexes}
    assert "ix_evidence_links_statement_id" in idx_names
    idx_names = {i.name for i in tables["statements"].indexes}
    assert "ix_statements_run_status" in idx_names
    idx_names = {i.name for i in tables["runs"].indexes}
    assert "ix_runs_tenant_status" in idx_names


def test_key_unique_constraints_defined() -> None:
    tables = Base.metadata.tables
    assert "uq_sources_content_hash" in {c.name for c in tables["sources"].constraints}
    assert "uq_passages_source_seq" in {c.name for c in tables["passages"].constraints}
    assert "uq_checkpoints_run_stage" in {c.name for c in tables["checkpoints"].constraints}


def test_evidence_links_fk_count() -> None:
    fks = {fk.target_fullname for fk in Base.metadata.tables["evidence_links"].foreign_keys}
    assert "passages.id" in fks
    assert "statements.id" in fks
    assert "runs.id" in fks
