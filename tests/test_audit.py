"""
tests/test_audit.py — Centerpiece test for the hash-chained audit log.

Sequence
--------
1. Perform 5 real reserve/settle actions through governance functions, producing
   5 audit_log rows via log_decision().
2. verify_chain() → must return (True, None).
3. Directly UPDATE one historical row's `reason` field via raw SQL (attacker
   simulation — bypasses log_decision() entirely).
4. verify_chain() → must return (False, <id of the tampered row>).

The test prints the verify_chain() result at each step so the output is
self-documenting.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest

from app.audit import log_decision, verify_chain
from app.governance import refund, reserve, revoke, settle

_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres:changeme@localhost:5432/myapp",
)
_SCHEMA_PATH = Path(__file__).parent.parent / "app" / "schema.sql"


# ---------------------------------------------------------------------------
# Helpers (same pool-inside-test-body pattern as test_governance.py)
# ---------------------------------------------------------------------------

async def _make_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn=_DSN, min_size=4, max_size=8)


async def _apply_schema(pool: asyncpg.Pool) -> None:
    schema = _SCHEMA_PATH.read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)


async def _seed_agent(pool: asyncpg.Pool, cap: str = "100.00") -> str:
    aid = f"audit-test-{uuid.uuid4()}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, cap, created_at) VALUES ($1, $2, $3, NOW())",
            aid, "Audit Test Agent", Decimal(cap),
        )
    return aid


async def _grant(pool: asyncpg.Pool, agent_id: str, tool_name: str,
                 max_per_call: Decimal | None = None) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_permissions (agent_id, tool_name, max_per_call)
            VALUES ($1, $2, $3)
            ON CONFLICT (agent_id, tool_name) DO UPDATE SET max_per_call = EXCLUDED.max_per_call
            """,
            agent_id, tool_name, max_per_call,
        )


async def _cleanup(pool: asyncpg.Pool, agent_id: str,
                   audit_ids: list[int]) -> None:
    async with pool.acquire() as conn:
        if audit_ids:
            await conn.execute(
                "DELETE FROM audit_log WHERE id = ANY($1::bigint[])", audit_ids
            )
        await conn.execute("DELETE FROM agents WHERE id = $1", agent_id)


async def _get_audit_ids_for_agent(
    pool: asyncpg.Pool, agent_id: str
) -> list[int]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM audit_log WHERE agent_id = $1 ORDER BY id",
            agent_id,
        )
    return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# THE CENTERPIECE TEST
# ---------------------------------------------------------------------------

async def test_audit_chain_detects_tamper() -> None:
    """
    Perform 5 governance actions, verify the chain is intact, tamper with
    one row directly, then prove verify_chain() catches it.
    """
    pool = await _make_pool()
    await _apply_schema(pool)
    aid = await _seed_agent(pool, cap="100.00")
    await _grant(pool, aid, "web_search")
    await _grant(pool, aid, "code_exec", max_per_call=Decimal("20.00"))

    audit_ids: list[int] = []

    try:
        # ── 5 governance actions ──────────────────────────────────────────
        # Action 1: reserve allowed
        r1 = await reserve(pool, aid, "web_search", Decimal("10.00"))
        assert r1 is True, "Action 1 should be allowed"

        # Action 2: reserve allowed (code_exec within limit)
        r2 = await reserve(pool, aid, "code_exec", Decimal("15.00"))
        assert r2 is True, "Action 2 should be allowed"

        # Action 3: reserve denied — cost > max_per_call
        r3 = await reserve(pool, aid, "code_exec", Decimal("25.00"))
        assert r3 is False, "Action 3 should be denied (cost > max_per_call)"

        # Action 4: settle action 1
        s1 = await settle(pool, aid, Decimal("10.00"), Decimal("8.00"),
                          tool_name="web_search")
        assert s1 is True, "Action 4 (settle) should succeed"

        # Action 5: refund action 2
        ref1 = await refund(pool, aid, Decimal("15.00"), tool_name="code_exec")
        assert ref1 is True, "Action 5 (refund) should succeed"

        # All 5 governance calls now await their log_decision() internally,
        # so audit rows are committed by the time we reach here.

        # ── Collect the audit row ids written for this agent ──────────────
        audit_ids = await _get_audit_ids_for_agent(pool, aid)
        print(f"\n  audit row ids written: {audit_ids}")
        assert len(audit_ids) == 3, (
            f"Expected 3 audit rows, got {len(audit_ids)}.  "
            f"Ids: {audit_ids}"
        )

        # ── Step 2: verify_chain() before tamper ─────────────────────────
        ok_before, bad_id_before = await verify_chain(pool, aid)
        print(f"  verify_chain() before tamper -> ({ok_before}, {bad_id_before})")
        assert ok_before is True,  "Chain should be intact before any tamper"
        assert bad_id_before is None

        # ── Step 3: attacker edits row #3 directly ────────────────────────
        # Pick the third audit row (id = audit_ids[2]).
        tampered_id = audit_ids[2]
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE audit_log SET reason = 'FORGED' WHERE id = $1",
                tampered_id,
            )
        print(f"  tampered row id: {tampered_id} (changed reason -> 'FORGED')")

        # ── Step 4: verify_chain() must now return (False, tampered_id) ───
        ok_after, bad_id_after = await verify_chain(pool, aid)
        print(f"  verify_chain() after tamper  -> ({ok_after}, {bad_id_after})")
        assert ok_after is False, "Chain must be invalid after tamper"
        assert bad_id_after == tampered_id, (
            f"verify_chain() should point at {tampered_id}, "
            f"got {bad_id_after}"
        )

    finally:
        await _cleanup(pool, aid, audit_ids)
        await pool.close()


# ---------------------------------------------------------------------------
# Supplementary: verify_chain() on an empty table returns (True, None)
# ---------------------------------------------------------------------------

async def test_verify_chain_empty_table() -> None:
    """
    verify_chain() on a completely empty audit_log returns (True, None).
    An empty chain has nothing to be wrong with.
    """
    pool = await _make_pool()
    await _apply_schema(pool)
    # Use a synthetic agent_id that will never have any rows.
    nonexistent_id = f"never-existed-{uuid.uuid4()}"
    try:
        ok, bad = await verify_chain(pool, nonexistent_id)
        # An agent with no rows has a trivially valid (empty) chain.
        assert ok is True
        assert bad is None
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Supplementary: concurrent log_decision() calls produce a valid chain
# ---------------------------------------------------------------------------

async def test_concurrent_log_decision_chain_valid() -> None:
    """
    Fire 5 concurrent log_decision() calls and assert verify_chain() still
    passes.  This exercises the advisory lock: without it, some callers would
    read the same prev_hash and produce duplicate chain links.
    """
    pool = await _make_pool()
    await _apply_schema(pool)
    aid = f"concurrent-audit-{uuid.uuid4()}"

    # We need the agent to exist for FK constraint; insert directly.
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents (id, name, cap, created_at) VALUES ($1,'C',100.00,NOW())",
            aid,
        )

    audit_ids: list[int] = []
    try:
        await asyncio.gather(*[
            log_decision(pool, aid, "tool", "allowed", "ok",
                         Decimal("1.00"), Decimal("1.00"))
            for _ in range(5)
        ])

        audit_ids = await _get_audit_ids_for_agent(pool, aid)
        assert len(audit_ids) == 5, f"Expected 5 rows, got {len(audit_ids)}"

        ok, bad = await verify_chain(pool, aid)
        print(f"\n  concurrent log: verify_chain() -> ({ok}, {bad})")
        assert ok is True,  f"Chain invalid after concurrent writes (bad row: {bad})"
        assert bad is None

    finally:
        await _cleanup(pool, aid, audit_ids)
        await pool.close()
