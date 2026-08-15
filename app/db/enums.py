"""Domain enums for the provenance core.

Stored as plain strings in the DB (native_enum=False semantics) with
CheckConstraints, so the schema stays migration-friendly while Python code gets
type-safe enum values.
"""

from enum import StrEnum


class RunStatus(StrEnum):
    SUBMITTED = "submitted"
    PLANNING = "planning"
    SEARCHING = "searching"
    COLLECTING = "collecting"
    STORING = "storing"
    EXTRACTING = "extracting"
    COMPARING = "comparing"
    VERIFYING = "verifying"
    DETECTING = "detecting"
    CONCLUDING = "concluding"
    TRACING = "tracing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class SourceType(StrEnum):
    WEB = "web"
    PDF = "pdf"
    RSS = "rss"
    DOCX = "docx"
    RTF = "rtf"
    UPLOAD = "upload"
    OTHER = "other"


class SourceStatus(StrEnum):
    PENDING = "pending"
    FETCHED = "fetched"
    FAILED = "failed"
    NORMALIZED = "normalized"
    QUARANTINED = "quarantined"


class StatementStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"
    QUARANTINED = "quarantined"


class EvidenceScore(StrEnum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"


class ContradictionStatus(StrEnum):
    FLAGGED = "flagged"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class EvidenceTier(StrEnum):
    T1 = "t1"
    T2 = "t2"
    T3 = "t3"
    T4 = "t4"
