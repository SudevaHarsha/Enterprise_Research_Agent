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
    """Ensure asyncpg driver and SSL for Railway connections."""
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if "railway.internal" in url and "sslmode=" not in url:
        sep = "&" if "?" in url else "?"
        url += f"{sep}sslmode=require"
    return url


def build_engine(url: str | None = None) -> AsyncEngine:
    db_url = _normalize_db_url(url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL)
    return create_async_engine(db_url, pool_pre_ping=True)


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
