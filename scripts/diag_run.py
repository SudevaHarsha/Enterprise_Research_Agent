"""Quick diagnostic: passages for a run."""
import asyncio, sys
from app.db.session import async_session_factory
from sqlalchemy import text

run_id = sys.argv[1] if len(sys.argv) > 1 else None
if not run_id:
    print("Usage: python scripts/diag_run.py <run_id>")
    sys.exit(1)

async def main():
    async with async_session_factory() as s:
        rows = (await s.execute(text(
            "SELECT p.id, p.source_id, substr(p.text, 1, 500), p.seq "
            "FROM passages p JOIN sources src ON p.source_id = src.id "
            "WHERE src.run_id = :rid ORDER BY p.id"
        ), {"rid": run_id})).fetchall()
        print("=== PASSAGES ===")
        for row in rows:
            print(f"  [{row[3]}] src={row[1]}")
            print(f"  text={row[2]}")
            print()
        if not rows:
            print("  (none)")

asyncio.run(main())
