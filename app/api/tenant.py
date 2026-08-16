"""Tenant scoping for the evaluator API (task_012).

Every run-scoped endpoint is filtered by the tenant resolved from the
``X-Tenant-ID`` request header (default ``"default"``). The API layer enforces
``tenant_id`` equality in code — cross-tenant access returns 403 (never 404, so
another tenant's resource existence is not leaked) — as defense in depth.

Postgres RLS hook (documented for the production migration): when the session
factory is created for a request, run ``SET LOCAL app.tenant_id = '<namespace>'``
and add a Postgres row-level-security policy filtering ``runs.tenant_id`` via
the resolved tenant for ``current_setting('app.tenant_id')``. Until that policy
is applied, the application-level checks in ``app.api.routes`` remain the
enforcement point.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select

from app.api.deps import get_session_factory
from app.db.models import Tenant
from app.pipeline.context import SessionFactory


async def get_tenant_id(
    session_factory: Annotated[SessionFactory, Depends(get_session_factory)],
    x_tenant_id: Annotated[str, Header()] = "default",
) -> UUID:
    """Resolve the ``X-Tenant-ID`` namespace to a Tenant row id.

    Unknown namespaces return 403 — the namespace may exist but is not yours,
    so its existence must not be observable (mirrors the cross-tenant 403
    contract).
    """
    async with session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.namespace == x_tenant_id))
    if tenant is None:
        raise HTTPException(status_code=403, detail=f"unknown tenant namespace {x_tenant_id!r}")
    return tenant.id
