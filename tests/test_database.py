"""Integration tests against a real PostgreSQL via Testcontainers.

Requires a running Docker daemon. Verifies the migration applies cleanly to a
fresh Postgres, the FK/index/trigger inventory is intact, append-only holds at
both the ORM and the database level, and the tenant seed is idempotent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.postgres import PostgresContainer

from alembic import command
from app.db import models
from app.db.base import AppendOnlyViolation

REPO_ROOT = Path(__file__).resolve().parents[1]

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

EXPECTED_INDEXES = {
    "ix_evidence_links_statement_id",
    "ix_audit_trace_run_id",
    "ix_statements_run_status",
    "ix_runs_tenant_status",
    "ix_kv_cache_expires_at",
}

EXPECTED_TRIGGERS = {"trg_evidence_links_append_only", "trg_audit_trace_append_only"}


@pytest.fixture(scope="module")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine") as postgres:
        yield postgres


@pytest.fixture(scope="module")
def database_url(postgres_container: PostgresContainer) -> str:
    url = postgres_container.get_connection_url()
    url = url.replace("+psycopg2", "", 1)
    return url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def migrated(database_url: str, monkeypatch_module: pytest.MonkeyPatch) -> str:
    monkeypatch_module.setenv("DATABASE_URL", database_url)
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(cfg, "head")
    return database_url


@pytest.fixture
async def session_factory(migrated: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_applies_all_tables(migrated: str) -> None:
    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
            tables = {row[0] for row in result}
            assert tables >= EXPECTED_TABLES, f"missing: {EXPECTED_TABLES - tables}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fk_index_trigger_inventory(migrated: str) -> None:
    engine = create_async_engine(migrated)
    try:
        async with engine.connect() as conn:
            fk_count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM pg_constraint "
                        "WHERE contype='f' AND connamespace='public'::regnamespace"
                    )
                )
            ).scalar()
            assert fk_count == 20, f"expected 20 FKs, got {fk_count}"

            idx_rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname='public'")
            )
            indexes = {row[0] for row in idx_rows}
            assert indexes >= EXPECTED_INDEXES

            tg_rows = await conn.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            )
            triggers = {row[0] for row in tg_rows}
            assert triggers >= EXPECTED_TRIGGERS, f"missing: {EXPECTED_TRIGGERS - triggers}"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_model_roundtrip_with_trace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant = models.Tenant(name="t1", namespace=f"ns-{uuid4().hex}", rbac_policy={})
        session.add(tenant)
        await session.flush()

        run = models.Run(
            tenant_id=tenant.id,
            question="How is AI transforming retail operations?",
            status=models.RunStatus.SUBMITTED.value,
        )
        session.add(run)
        await session.flush()

        source = models.Source(
            run_id=run.id,
            uri="https://example.com/report",
            title="Sample Report",
            source_type=models.SourceType.WEB.value,
            content_hash=uuid4().hex,
            allowlisted_uri=True,
        )
        session.add(source)
        await session.flush()

        passage = models.Passage(
            source_id=source.id, seq=0, text="AI reduces store labor.", hash=uuid4().hex
        )
        session.add(passage)
        await session.flush()

        statement = models.Statement(
            passage_id=passage.id,
            run_id=run.id,
            text="AI reduces store labor.",
            status=models.StatementStatus.DRAFT.value,
        )
        session.add(statement)
        await session.flush()

        link = models.EvidenceLink(
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=run.id,
            score=models.EvidenceScore.FULL.value,
            method="support_matrix",
        )
        session.add(link)
        await session.commit()

        fresh = await session.get(models.Statement, statement.id)
        assert fresh is not None and fresh.status == "draft"
        assert run.status == "submitted"


@pytest.mark.asyncio
async def test_statement_defaults_to_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant = models.Tenant(name="t", namespace=f"ns-{uuid4().hex}", rbac_policy={})
        session.add(tenant)
        await session.flush()
        run = models.Run(tenant_id=tenant.id, question="q")
        session.add(run)
        await session.flush()
        source = models.Source(
            run_id=run.id, uri="https://x.test/a", content_hash=uuid4().hex, source_type="web"
        )
        session.add(source)
        await session.flush()
        passage = models.Passage(source_id=source.id, seq=0, text="t", hash=uuid4().hex)
        session.add(passage)
        await session.flush()
        statement = models.Statement(passage_id=passage.id, run_id=run.id, text="t")
        session.add(statement)
        await session.commit()
        assert statement.status == models.StatementStatus.DRAFT.value


@pytest.mark.asyncio
async def test_orm_append_only_update_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    link = await _create_evidence_link(session_factory)
    async with session_factory() as session:
        fresh = await session.get(models.EvidenceLink, link.id)
        assert fresh is not None
        fresh.score = "partial"
        with pytest.raises(AppendOnlyViolation):
            await session.flush()


@pytest.mark.asyncio
async def test_orm_append_only_delete_blocked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    link = await _create_evidence_link(session_factory)
    async with session_factory() as session:
        fresh = await session.get(models.EvidenceLink, link.id)
        assert fresh is not None
        await session.delete(fresh)
        with pytest.raises(AppendOnlyViolation):
            await session.flush()


@pytest.mark.asyncio
async def test_db_trigger_blocks_direct_update(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant = models.Tenant(name="t", namespace=f"ns-{uuid4().hex}", rbac_policy={})
        session.add(tenant)
        await session.flush()
        run = models.Run(tenant_id=tenant.id, question="q")
        session.add(run)
        await session.commit()

        await session.execute(
            text(
                "INSERT INTO audit_trace "
                "(id, run_id, entity_type, entity_id, action, actor, decision, reason) "
                "VALUES (gen_random_uuid(), :run_id, 'statement', "
                "'00000000-0000-0000-0000-000000000000', 'insert', 'system', 'seed', 'fixture')"
            ).bindparams(run_id=run.id)
        )
        await session.commit()
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE audit_trace SET action = 'tamper' WHERE 1=1"))
        await session.rollback()


@pytest.mark.asyncio
async def test_fk_violation_rejected(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        orphan = models.Run(tenant_id=uuid4(), question="orphan run")
        session.add(orphan)
        with pytest.raises((IntegrityError, OperationalError)):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_checkpoint_unique_per_stage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        tenant = models.Tenant(name="t", namespace=f"ns-{uuid4().hex}", rbac_policy={})
        session.add(tenant)
        await session.flush()
        run = models.Run(tenant_id=tenant.id, question="q")
        session.add(run)
        await session.flush()
        session.add(models.Checkpoint(run_id=run.id, stage="stage_define", state={"x": 1}))
        await session.commit()
        session.add(models.Checkpoint(run_id=run.id, stage="stage_define", state={"x": 2}))
        with pytest.raises((IntegrityError, OperationalError)):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_seed_idempotent(session_factory: async_sessionmaker[AsyncSession]) -> None:
    from sqlalchemy import select

    from app.db.seed import seed_tenant

    ns = f"seed-{uuid4().hex}"
    assert await seed_tenant("First", ns, factory=session_factory) is True
    assert await seed_tenant("Second run", ns, factory=session_factory) is False

    async with session_factory() as session:
        count = (
            (await session.execute(select(models.Tenant).where(models.Tenant.namespace == ns)))
            .scalars()
            .all()
        )
        assert len(count) == 1


async def _create_evidence_link(
    session_factory: async_sessionmaker[AsyncSession],
) -> models.EvidenceLink:
    async with session_factory() as session:
        tenant = models.Tenant(name="t", namespace=f"ns-{uuid4().hex}", rbac_policy={})
        session.add(tenant)
        await session.flush()
        run = models.Run(tenant_id=tenant.id, question="q")
        session.add(run)
        await session.flush()
        source = models.Source(
            run_id=run.id, uri="https://x.test/a", content_hash=uuid4().hex, source_type="web"
        )
        session.add(source)
        await session.flush()
        passage = models.Passage(source_id=source.id, seq=0, text="t", hash=uuid4().hex)
        session.add(passage)
        await session.flush()
        statement = models.Statement(passage_id=passage.id, run_id=run.id, text="t")
        session.add(statement)
        await session.flush()
        link = models.EvidenceLink(
            statement_id=statement.id,
            passage_id=passage.id,
            run_id=run.id,
            score="full",
            method="test",
        )
        session.add(link)
        await session.commit()
        return link
