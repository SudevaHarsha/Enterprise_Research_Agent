"""Alembic migration environment.

Async engine (asyncpg) with the async-template pattern. Reads DATABASE_URL from
the environment, falling back to the local dev URL. ``target_metadata`` is the
ECRKE provenance core so ``alembic revision --autogenerate`` stays in sync with
``app.db.models``.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config, create_async_engine

from alembic import context
from app.db import models  # noqa: F401  (import to register tables on metadata)
from app.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

_raw_url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url") or "")
# Ensure the asyncpg driver is present (Railway provides plain postgresql://)
if _raw_url.startswith("postgresql://"):
    _raw_url = _raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
# Railway Postgres requires SSL — add sslmode=require if not already present
if "railway.internal" in _raw_url and "sslmode=" not in _raw_url:
    sep = "&" if "?" in _raw_url else "?"
    _raw_url += f"{sep}sslmode=require"
config.set_main_option("sqlalchemy.url", _raw_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    url = config.get_main_option("sqlalchemy.url")
    connectable = create_async_engine(url, poolclass=None)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
