"""Database access: provenance-core models, async engine/session, migrations.

Submodules
----------
enums      : type-safe status/enum values stored as constrained strings
base       : DeclarativeBase, naming convention, append-only enforcement
models     : the 14-table relational provenance core (design doc §7.1)
session    : async engine + session factory (asyncpg)
seed       : idempotent seed script for tenants
"""
