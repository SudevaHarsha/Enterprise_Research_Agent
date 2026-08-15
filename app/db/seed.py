"""Idempotent seed script for the provenance core.

Usage:
    python -m app.db.seed [--name "Default Tenant"] [--namespace default]

Creates a tenant if its namespace does not already exist. Safe to run repeatedly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import models  # noqa: F401
from app.db.session import async_session_factory


async def seed_tenant(
    name: str,
    namespace: str,
    factory: async_sessionmaker[AsyncSession] | None = None,
) -> bool:
    session_factory = factory or async_session_factory
    async with session_factory() as session:
        existing = (
            await session.execute(select(models.Tenant).where(models.Tenant.namespace == namespace))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"[seed] tenant '{namespace}' already exists (id={existing.id}); skipped.")
            return False
        tenant = models.Tenant(name=name, namespace=namespace, rbac_policy={})
        session.add(tenant)
        await session.commit()
        print(f"[seed] created tenant '{namespace}' (id={tenant.id}).")
        return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed ECRKE provenance core.")
    parser.add_argument("--name", default="Default Tenant")
    parser.add_argument("--namespace", default="default")
    args = parser.parse_args()
    return 0 if asyncio.run(seed_tenant(args.name, args.namespace)) else 0


if __name__ == "__main__":
    sys.exit(main())
