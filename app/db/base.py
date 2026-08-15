"""Declarative base, naming conventions, and append-only enforcement.

Write governance (design doc §7.2, §9.3): ``evidence_links`` and
``audit_trace`` are append-only — no in-place edits, no deletes. Enforcement is
belt-and-braces: a SQLAlchemy ORM-level guard here, plus Postgres triggers in the
migration (defense in depth).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, MetaData, Uuid, event, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Mapper, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class AppendOnlyViolation(RuntimeError):
    """Raised on any attempt to UPDATE or DELETE an append-only row."""


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


APPEND_ONLY_MODELS: set[type[Base]] = set()


def _block_update_or_delete(mapper: Mapper[Base], connection: object, target: Base) -> None:
    raise AppendOnlyViolation(
        f"{mapper.class_.__name__} is append-only: in-place UPDATE/DELETE is forbidden. "
        "Versioning and correction happen via new rows (design doc §7.2)."
    )


def register_append_only(model: type[Base]) -> None:
    """Register ORM-level guards so append-only tables cannot be edited via the session."""
    event.listen(model, "before_update", _block_update_or_delete)
    event.listen(model, "before_delete", _block_update_or_delete)
    APPEND_ONLY_MODELS.add(model)


def iter_append_only_models() -> Iterator[type[Base]]:
    return iter(APPEND_ONLY_MODELS)
