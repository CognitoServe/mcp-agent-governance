"""Test whether asyncpg TIMESTAMPTZ round-trip preserves isoformat() exactly."""
import asyncio, os, hashlib, asyncpg
from datetime import datetime, timezone
from decimal import Decimal

GENESIS = "0" * 64
DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/test_db")

async def main():
    pool = await asyncpg.create_pool(DSN)

    # Insert one row manually with a known ts
    ts_in = datetime.now(tz=timezone.utc)
    payload = f"{GENESIS}|myagent|mytool|allowed|ok|5.00|None|{ts_in.isoformat()}"
    rh = hashlib.sha256(payload.encode()).hexdigest()

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('probe'))")
            await conn.execute(
                "INSERT INTO audit_log (agent_id, tool_name, decision, reason, est_cost, actual_cost, ts, prev_hash, row_hash) "
                "VALUES ('myagent', 'mytool', 'allowed', 'ok', 5.00, NULL, $1, $2, $3)",
                ts_in, GENESIS, rh,
            )

    # Read it back
    row = await pool.fetchrow("SELECT ts, prev_hash, row_hash, est_cost, actual_cost FROM audit_log WHERE agent_id='myagent' ORDER BY id DESC LIMIT 1")
    ts_out = row["ts"]
    print(f"ts_in  = {ts_in!r}")
    print(f"ts_out = {ts_out!r}")
    print(f"ts_in.isoformat()  = {ts_in.isoformat()}")
    print(f"ts_out.isoformat() = {ts_out.isoformat()}")
    print(f"equal?             = {ts_in.isoformat() == ts_out.isoformat()}")
    print(f"est_cost  = {row['est_cost']!r}  (type={type(row['est_cost']).__name__})")
    print(f"actual_cost={row['actual_cost']!r}  (type={type(row['actual_cost']).__name__})")

    # Recompute hash with ts_out
    payload2 = f"{GENESIS}|myagent|mytool|allowed|ok|{row['est_cost']}|{row['actual_cost']}|{ts_out.isoformat()}"
    rh2 = hashlib.sha256(payload2.encode()).hexdigest()
    print(f"original hash: {rh}")
    print(f"recomputed   : {rh2}")
    print(f"match?         {rh == rh2}")
    print(f"stored hash:   {row['row_hash']}")

    # Original payload
    print(f"\noriginal payload: {payload!r}")
    print(f"recomp   payload: {payload2!r}")

    await pool.execute("DELETE FROM audit_log WHERE agent_id='myagent'")
    await pool.close()

asyncio.run(main())
