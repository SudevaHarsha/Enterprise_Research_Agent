"""Async SQLAlchemy engine and session factory.

DATABASE_URL is read from the environment (see ``app.core.config`` in task_003;
a safe local default keeps the dev loop working without a full config layer).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://ecrke:ecrke_dev@localhost:5433/ecrke"


def _normalize_db_url(url: str) -> str:
    """Ensure asyncpg driver for PostgreSQL connections."""
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def build_engine(url: str | None = None) -> AsyncEngine:
    import ssl as _ssl

    db_url = _normalize_db_url(url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL)
    connect_args: dict = {}
    if "railway.internal" in db_url:
        ctx = _ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
        connect_args["ssl"] = ctx
    return create_async_engine(db_url, pool_pre_ping=True, connect_args=connect_args)


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


engine = build_engine()
async_session_factory = build_session_factory(engine)


async def session_scope() -> AsyncIterator[AsyncSession]:
    """Context manager dependency: yields a session, rolls back on error, closes always."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
