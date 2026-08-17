"""Check checkpoint and passage status for a run."""
import asyncio, sys
from app.db.session import async_session_factory
from sqlalchemy import text

run_id = sys.argv[1] if len(sys.argv) > 1 else None
if not run_id:
    print("Usage: python scripts/diag_checkpoints.py <run_id>")
    sys.exit(1)

async def main():
    async with async_session_factory() as s:
        # Check checkpoint store
        rows = await s.execute(text("SELECT key, payload FROM kv_cache WHERE key LIKE :p"), {"p": f"%{run_id}%"})
        print("=== CHECKPOINT KV ENTRIES ===")
        for r in rows:
            print(f"  {r[0]}: {r[1]}")
        print()

        # Check source statuses for this run
        rows2 = await s.execute(text("SELECT id, status, source_type FROM sources WHERE run_id = :rid"), {"rid": run_id})
        print("=== SOURCE STATUSES ===")
        for r in rows2:
            print(f"  {r[0]}  status={r[1]}  type={r[2]}")
        print()

        # Check passages per source for this run
        rows3 = await s.execute(text(
            "SELECT p.source_id, count(*) as cnt "
            "FROM passages p JOIN sources s ON p.source_id = s.id "
            "WHERE s.run_id = :rid GROUP BY p.source_id"
        ), {"rid": run_id})
        print("=== PASSAGES PER SOURCE ===")
        for r in rows3:
            print(f"  source={r[0]}  count={r[1]}")
        print()

        # Check statements for this run
        rows4 = await s.execute(text("SELECT count(*) FROM statements WHERE run_id = :rid"), {"rid": run_id})
        print(f"Statements for run: {rows4.fetchone()[0]}")

        # Check runs table checkpoint field
        rows5 = await s.execute(text("SELECT checkpoint, status, stage FROM runs WHERE id = :rid"), {"rid": run_id})
        r5 = rows5.fetchone()
        if r5:
            print(f"\n=== RUN ROW ===")
            print(f"  checkpoint: {r5[0]}")
            print(f"  status: {r5[1]}")
            print(f"  stage: {r5[2]}")

asyncio.run(main())
