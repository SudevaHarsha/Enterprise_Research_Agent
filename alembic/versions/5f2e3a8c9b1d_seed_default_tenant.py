"""seed default tenant

Revision ID: 5f2e3a8c9b1d
Revises: 8a6a416af425
Create Date: 2026-08-16 14:20:00.000000

Seeds the framework-default tenant (namespace ``default``) so a fresh clone
with ``docker compose up`` has a working tenant out of the box — the DoD
run surfaced that migrations created the ``tenants`` table but nothing ever
inserted a row, so the README's first ``POST /v1/runs`` failed with 403
``unknown tenant namespace``. The insert is idempotent
(``ON CONFLICT (namespace) DO NOTHING``) so it is safe on every boot.

The fixed id ``00000000-0000-0000-0000-000000000001`` is the demo tenant id
referenced by the README/demo-script walkthroughs.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "5f2e3a8c9b1d"
down_revision: str | None = "8a6a416af425"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO tenants (id, name, namespace, created_at)
            VALUES (
                '00000000-0000-0000-0000-000000000001',
                'Default tenant',
                'default',
                now()
            )
            ON CONFLICT (namespace) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM tenants WHERE namespace = 'default'"))
