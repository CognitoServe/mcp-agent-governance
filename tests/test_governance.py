"""
tests/test_governance.py — correctness and concurrency tests for governance.py.

Design notes (applies to every test in this file)
--------------------------------------------------
• asyncpg binds its pool's socket-level Futures to the event loop active at
  create_pool() time.  pytest-asyncio 0.24 (asyncio_mode=auto) gives each
  async test its own event loop, so a pool created in a fixture can end up
  on a different loop than the test — causing "attached to a different loop".
  Fix: create and close the pool *inside* the test body.

• We do NOT use the rollback-per-test fixture.  The atomic UPDATE path only
  protects committed transactions; rolling back would make tests trivially pass.

• Each test inserts uuid-named rows and deletes them in a finally block, so
  runs are fully isolated even when the suite is run multiple times against the
  same database.

• _seed_agent() calls the full schema SQL (idempotent CREATE TABLE IF NOT EXISTS
  + idempotent ALTER TABLE … ADD CONSTRAINT IF NOT EXISTS) so every test is
  self-bootstrapping: no external migration step required.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.governance import activate, refund, reserve, revoke, settle

# ---------------------------------------------------------------------------
# Shared config
# ---------------------------------------------------------------------------

_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/test_db",
)

_SCHEMA_PATH = Path(__file__).parent.parent / "app" / "schema.sql"


def _schema() -> str:
    return _SCHEMA_PATH.read_text()


async def _make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=12, max_size=20)


async def _seed_agent(pool: asyncpg.Pool, cap: str = "50.00") -> str:
    """Insert a fresh agent, apply schema (idempotent), return agent id."""
    aid = f"test-{uuid.uuid4()}"
    async with pool.acquire() as conn:
        await conn.execute(_schema())
        await conn.execute(
            "INSERT INTO agents (id, name, cap, created_at) VALUES ($1, $2, $3, NOW())",
            aid, "Test Agent", Decimal(cap),
        )
    return aid


async def _grant(
    pool: asyncpg.Pool,
    agent_id: str,
    tool_name: str,
    max_per_call: Decimal | None = None,
) -> None:
    """Insert a permission row for (agent_id, tool_name)."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_permissions (agent_id, tool_name, max_per_call)
            VALUES ($1, $2, $3)
            ON CONFLICT (agent_id, tool_name) DO UPDATE SET max_per_call = EXCLUDED.max_per_call
            """,
            agent_id, tool_name, max_per_call,
        )


async def _read(pool: asyncpg.Pool, aid: str) -> asyncpg.Record:
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT spent, reserved, cap FROM agents WHERE id = $1", aid
        )


async def _cleanup(pool: asyncpg.Pool, aid: str) -> None:
    async with pool.acquire() as conn:
        # agent_permissions rows cascade-delete via FK
        await conn.execute("DELETE FROM agents WHERE id=$1", aid)


# ---------------------------------------------------------------------------
# Test 1 — Concurrency: reserve() race condition
# ---------------------------------------------------------------------------

async def test_concurrent_reserve_respects_cap() -> None:
    """
    Fire 10 concurrent reserve(cost=15.00) calls against a 50.00 cap.

    At most 3 may succeed (3×15=45 ≤ 50; 4th would need 60 > 50).
    The DB-state assertion is the definitive proof: no race can breach the cap.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "web_search")
    try:
        results: list[bool] = await asyncio.gather(
            *[reserve(pool, aid, "web_search", Decimal("15.00")) for _ in range(10)]
        )

        successes = sum(results)
        row = await _read(pool, aid)
        total = row["spent"] + row["reserved"]

        print(f"\n  results={results}")
        print(f"  successes={successes}/10  spent={row['spent']}  "
              f"reserved={row['reserved']}  total={total}  cap={row['cap']}")

        assert successes <= 3, (
            f"RACE CONDITION: {successes} succeeded (max 3 allowed). "
            f"results={results}"
        )
        assert successes >= 1, "No reservation succeeded — check DB / schema."
        assert total <= row["cap"], (
            f"CAP BREACHED: spent={row['spent']} reserved={row['reserved']} "
            f"total={total} > cap={row['cap']}"
        )
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 2 — Normal settle: partial cost refunds headroom correctly
# ---------------------------------------------------------------------------

async def test_settle_normal_case() -> None:
    """
    reserve(15) → settle(reserved=15, actual=12).

    Expected DB state after settle:
    • spent    increased by exactly 12  (0 → 12)
    • reserved decreased by exactly 15  (15 → 0)
    • 38 units of headroom remain (cap=50 − spent=12 − reserved=0)
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "web_search")
    try:
        before = await _read(pool, aid)
        assert before["spent"]    == Decimal("0.00")
        assert before["reserved"] == Decimal("0.00")

        ok = await reserve(pool, aid, "web_search", Decimal("15.00"))
        assert ok, "reserve(15) should succeed on a fresh 50-cap agent"
        after_reserve = await _read(pool, aid)
        assert after_reserve["reserved"] == Decimal("15.00")
        assert after_reserve["spent"]    == Decimal("0.00")

        ok = await settle(pool, aid, Decimal("15.00"), Decimal("12.00"))
        assert ok, "settle() should return True"

        after_settle = await _read(pool, aid)
        print(f"\n  after settle: spent={after_settle['spent']}  "
              f"reserved={after_settle['reserved']}  cap={after_settle['cap']}")

        assert after_settle["spent"]    == Decimal("12.00"), \
            f"spent should be 12.00, got {after_settle['spent']}"
        assert after_settle["reserved"] == Decimal("0.00"), \
            f"reserved should be 0.00, got {after_settle['reserved']}"
        headroom = after_settle["cap"] - after_settle["spent"] - after_settle["reserved"]
        assert headroom == Decimal("38.00"), \
            f"Expected 38.00 headroom, got {headroom}"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 3 — Full-failure refund: reserved returns to prior value, spent unchanged
# ---------------------------------------------------------------------------

async def test_refund_full_failure() -> None:
    """
    reserve(15) → refund(15).

    The tool failed entirely: reserved goes back to 0, spent stays 0.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "web_search")
    try:
        before = await _read(pool, aid)

        ok = await reserve(pool, aid, "web_search", Decimal("15.00"))
        assert ok
        mid = await _read(pool, aid)
        assert mid["reserved"] == Decimal("15.00")
        assert mid["spent"]    == Decimal("0.00")

        ok = await refund(pool, aid, Decimal("15.00"))
        assert ok, "refund() should return True"

        after = await _read(pool, aid)
        print(f"\n  after refund: spent={after['spent']}  "
              f"reserved={after['reserved']}  cap={after['cap']}")

        assert after["reserved"] == before["reserved"], \
            f"reserved should be {before['reserved']}, got {after['reserved']}"
        assert after["spent"] == before["spent"], \
            f"spent should be {before['spent']}, got {after['spent']}"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 4 — Overrun edge case now raises CheckViolationError
# ---------------------------------------------------------------------------

async def test_settle_overrun_raises_constraint_error() -> None:
    """
    reserve(15) on a cap=20 agent → settle(reserved=15, actual=25).

    Before the spent_reserved_within_cap CHECK was added, this silently
    succeeded.  Now Postgres raises CheckViolationError with constraint name
    'spent_reserved_within_cap'.  The real error message (observed):

        asyncpg.exceptions.CheckViolationError:
            new row for relation \"agents\" violates check constraint
            \"spent_reserved_within_cap\"
        DETAIL:  Failing row contains (..., cap=20.00, spent=25.00, reserved=0.00, ...).

    This test pins that behaviour: if the constraint is ever dropped, the test
    fails immediately and forces a deliberate decision.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool, cap="20.00")
    await _grant(pool, aid, "web_search")
    try:
        ok = await reserve(pool, aid, "web_search", Decimal("15.00"))
        assert ok

        with pytest.raises(asyncpg.CheckViolationError) as exc_info:
            await settle(pool, aid, Decimal("15.00"), Decimal("25.00"))

        assert "spent_reserved_within_cap" in str(exc_info.value)
        print(f"\n  Raised (expected): {exc_info.value}")

        # DB row: Postgres rolled back the failed UPDATE, so state is unchanged
        # from just after reserve (reserved=15, spent=0).
        row = await _read(pool, aid)
        print(f"  DB after failed settle: spent={row['spent']}  "
              f"reserved={row['reserved']}  cap={row['cap']}")
        assert row["reserved"] == Decimal("15.00"), "reserved should be unchanged after failed settle"
        assert row["spent"]    == Decimal("0.00"),  "spent should be unchanged after failed settle"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 5 — Permission denied: no row in agent_permissions
# ---------------------------------------------------------------------------

async def test_reserve_denied_no_permission_row() -> None:
    """
    reserve() returns False when no permission row exists for the tool.
    The agent's balance must be completely untouched.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    # deliberately do NOT grant any permission for "secret_tool"
    try:
        before = await _read(pool, aid)

        result = await reserve(pool, aid, "secret_tool", Decimal("10.00"))

        after = await _read(pool, aid)
        print(f"\n  reserve (no perm) returned: {result}")
        print(f"  before: spent={before['spent']} reserved={before['reserved']}")
        print(f"  after:  spent={after['spent']}  reserved={after['reserved']}")

        assert result is False, "reserve() must return False when tool is not permitted"
        assert after["reserved"] == before["reserved"], \
            "reserved must not change when permission is denied"
        assert after["spent"] == before["spent"], \
            "spent must not change when permission is denied"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 6 — Permission denied: cost > max_per_call
# ---------------------------------------------------------------------------

async def test_reserve_denied_exceeds_max_per_call() -> None:
    """
    A permission row exists for the tool, but max_per_call=5.00 and cost=10.00.
    reserve() must return False without touching the balance.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "expensive_tool", max_per_call=Decimal("5.00"))
    try:
        before = await _read(pool, aid)

        result = await reserve(pool, aid, "expensive_tool", Decimal("10.00"))

        after = await _read(pool, aid)
        print(f"\n  reserve (cost>max_per_call) returned: {result}")
        print(f"  before: spent={before['spent']} reserved={before['reserved']}")
        print(f"  after:  spent={after['spent']}  reserved={after['reserved']}")

        assert result is False, \
            "reserve() must return False when cost > max_per_call"
        assert after["reserved"] == before["reserved"], "balance must be untouched"
        assert after["spent"]    == before["spent"],    "balance must be untouched"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 7 — Permission succeeds: max_per_call IS NULL (unrestricted)
# ---------------------------------------------------------------------------

async def test_reserve_succeeds_max_per_call_null() -> None:
    """
    max_per_call IS NULL means 'allowed, no per-call limit'.
    A large cost (45.00) must succeed against a cap=50 agent.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "unlimited_tool", max_per_call=None)
    try:
        result = await reserve(pool, aid, "unlimited_tool", Decimal("45.00"))

        row = await _read(pool, aid)
        print(f"\n  reserve (max_per_call=NULL, cost=45) returned: {result}")
        print(f"  after: spent={row['spent']}  reserved={row['reserved']}  cap={row['cap']}")

        assert result is True, \
            "reserve() must succeed when max_per_call IS NULL and cost <= cap"
        assert row["reserved"] == Decimal("45.00"), \
            f"reserved should be 45.00, got {row['reserved']}"
    finally:
        await _cleanup(pool, aid)
        await pool.close()


# ---------------------------------------------------------------------------
# Test 8 — Kill-switch: revoke() takes effect on the very next reserve() call
# ---------------------------------------------------------------------------

async def test_revoke_blocks_immediate_next_reserve() -> None:
    """
    reserve() succeeds → revoke() → reserve() again immediately returns False.

    This proves revocation is synchronous and takes effect on the very next
    call, not eventually.  There is no window where a revoked agent can
    still reserve budget.
    """
    pool = await _make_pool()
    aid = await _seed_agent(pool)
    await _grant(pool, aid, "web_search")
    try:
        # First reserve succeeds — agent is active
        r1 = await reserve(pool, aid, "web_search", Decimal("10.00"))
        assert r1 is True, "First reserve() should succeed on an active agent"

        row_mid = await _read(pool, aid)
        assert row_mid["reserved"] == Decimal("10.00")

        # Kill-switch
        revoked = await revoke(pool, aid)
        assert revoked is True, "revoke() should return True for an existing agent"

        # Immediate retry — must be denied
        r2 = await reserve(pool, aid, "web_search", Decimal("10.00"))

        row_after = await _read(pool, aid)
        print(f"\n  r1={r1}  revoked={revoked}  r2={r2}")
        print(f"  after revoke+reserve: reserved={row_after['reserved']}  "
              f"spent={row_after['spent']}  cap={row_after['cap']}")

        assert r2 is False, \
            "reserve() must return False immediately after revoke() — no grace window"
        assert row_after["reserved"] == Decimal("10.00"), \
            "reserved must not have changed after the denied second reserve()"
    finally:
        await _cleanup(pool, aid)
        await pool.close()
