"""
app/audit.py — Hash-chained, tamper-evident audit log.

Public API
----------
log_decision(pool, agent_id, tool_name, decision, reason,
             est_cost, actual_cost) -> None

verify_chain(pool, agent_id) -> tuple[bool, int | None]

Chain scope
-----------
Each agent maintains its own independent hash chain.  The first row for any
given agent uses prev_hash = GENESIS_HASH ('0'*64).  This means:
• verify_chain() is called per-agent, not globally.
• Rows for different agents do not interfere with each other's chains.
• An attacker who edits a row for agent-A does not affect agent-B's chain.

Design: why an advisory lock is required here but not in reserve()
------------------------------------------------------------------
reserve() can fold both its integrity check and its write into a single
UPDATE statement, so Postgres row-level locking makes the whole operation
atomic with no read-before-write gap.

A hash chain cannot be handled that way.  To compute this row's row_hash we
must first know the PREVIOUS row's row_hash for the same agent — that is a
read-before-write dependency that cannot be eliminated.  If two concurrent
callers both read the same "last row" for an agent, both compute prev_hash
from that row, and both INSERT, the chain will have two rows each claiming to
follow the same predecessor.  verify_chain() will detect the inconsistency,
but the damage is structural and permanent.

A Postgres advisory lock (pg_advisory_xact_lock) solves this cleanly.  The
lock is scoped to the enclosing transaction and released automatically on
commit or rollback.  Any other transaction trying to acquire the same lock
blocks until this one finishes.  We derive the lock integer from
hashtext(agent_id || ':audit_log_append') so that two agents serialise only
their own appends — they never block each other.

The advisory lock serialises only the log-append path per agent — it does not
affect reserve(), settle(), or any other code.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal

import asyncpg

# Sentinel prev_hash used for the very first row of any agent's chain.
GENESIS_HASH = "0" * 64


def _lock_key(agent_id: str) -> str:
    """Per-agent advisory lock key string (passed to hashtext() in Postgres)."""
    return f"{agent_id}:audit_log_append"


def _fmt_cost(value: Decimal | None) -> str:
    """
    Canonical string representation of a cost column for hashing.

    Postgres NUMERIC(12,2) always stores exactly 2 decimal places, so when
    asyncpg reads the value back it returns Decimal('0.00') not Decimal('0').
    Python code that passes Decimal('0') at write time would produce '0' in the
    payload, but verify_chain() would produce '0.00' — breaking the hash.

    Normalising to 2dp here keeps the payload identical at write and verify time.
    None is represented as the string 'None' (matching Python's default f-string
    formatting, which the DB does not change).
    """
    if value is None:
        return "None"
    return str(value.quantize(Decimal("0.01")))


def _compute_hash(
    prev_hash: str,
    agent_id: str,
    tool_name: str,
    decision: str,
    reason: str,
    est_cost: Decimal | None,
    actual_cost: Decimal | None,
    ts: datetime,
) -> str:
    """
    Produce the sha256 digest stored as audit_log.row_hash.

    Every field that appears in the row participates in the hash so that
    editing any column — including the timestamp — changes the digest and
    breaks the chain at that link.
    """
    payload = (
        f"{prev_hash}|{agent_id}|{tool_name}|{decision}|{reason}"
        f"|{_fmt_cost(est_cost)}|{_fmt_cost(actual_cost)}|{ts.isoformat()}"
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def log_decision(
    pool: asyncpg.Pool,
    agent_id: str,
    tool_name: str,
    decision: str,
    reason: str,
    est_cost: Decimal | None = None,
    actual_cost: Decimal | None = None,
) -> None:
    """
    Append one entry to the audit_log in a way that preserves the per-agent
    hash chain.

    Why the advisory lock is necessary here (but was not needed in reserve()):

    reserve() folds its check and its write into a single SQL UPDATE, so
    Postgres row-level locking makes the whole thing atomic — there is no gap
    between reading state and writing it.  Here we cannot do that: to compute
    this row's row_hash we must first READ the previous row's stored row_hash
    for the same agent, then WRITE the new row.  Two concurrent callers can
    both read the same "last row" at the same time, both compute their new
    row's prev_hash from it, and both INSERT — producing two rows that claim
    to continue from the same parent.  The chain is then permanently broken
    from that point forward.

    pg_advisory_xact_lock() acquires an integer lock that is held for the
    lifetime of the enclosing transaction and released automatically when the
    transaction commits or rolls back.  Any other transaction that tries to
    acquire the same lock will block until this one finishes.  We derive the
    lock key from the agent_id so that different agents never block each other
    — only concurrent appenders for the SAME agent are serialised.

    Parameters
    ----------
    decision : 'allowed' or 'denied'
    reason   : short code, e.g. 'ok', 'cap_exceeded', 'not_permitted', 'revoked'
    est_cost : the cost estimate passed to reserve()
    actual_cost : the actual cost recorded by settle()
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Per-agent advisory lock — serialises concurrent appenders for
            # this agent only.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))",
                _lock_key(agent_id),
            )

            # Capture timestamp strictly AFTER acquiring the lock so that concurrent
            # appenders receive sequentially distinct timestamps instead of the
            # exact same microsecond from when the tasks were initially scheduled.
            ts = datetime.now(tz=timezone.utc)

            # Read the most-recently committed row's hash for this agent, or
            # use the genesis sentinel if no rows exist yet.
            last = await conn.fetchrow(
                """
                SELECT row_hash
                  FROM audit_log
                 WHERE agent_id = $1
                 ORDER BY id DESC
                 LIMIT 1
                """,
                agent_id,
            )
            prev_hash = last["row_hash"] if last else GENESIS_HASH

            row_hash = _compute_hash(
                prev_hash, agent_id, tool_name, decision, reason,
                est_cost, actual_cost, ts,
            )

            await conn.execute(
                """
                INSERT INTO audit_log
                    (agent_id, tool_name, decision, reason,
                     est_cost, actual_cost, ts, prev_hash, row_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                agent_id, tool_name, decision, reason,
                est_cost, actual_cost, ts, prev_hash, row_hash,
            )
            # Transaction commits here, releasing the advisory lock.


async def verify_chain(
    pool: asyncpg.Pool,
    agent_id: str,
) -> tuple[bool, int | None]:
    """
    Walk every audit_log row for *agent_id* in insertion order and verify the
    hash chain.

    For each row we:
    1. Check that its prev_hash matches the previous row's stored row_hash
       (or GENESIS_HASH for the first row of this agent).
    2. Recompute the row's own row_hash from its columns and compare it to
       the stored row_hash.

    Returns
    -------
    (True,  None)    — chain is intact from genesis to the last row.
    (False, bad_id)  — the first row whose hash doesn't verify; bad_id is the
                       audit_log.id of the offending row.

    Any discrepancy means either a row's data was edited in-place, a row was
    inserted bypassing log_decision(), or a row was deleted (the next row's
    prev_hash will no longer match).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, agent_id, tool_name, decision, reason,
                   est_cost, actual_cost, ts, prev_hash, row_hash
              FROM audit_log
             WHERE agent_id = $1
             ORDER BY id ASC
            """,
            agent_id,
        )

    expected_prev = GENESIS_HASH

    for row in rows:
        # ── 1. Backward link ──────────────────────────────────────────────
        if row["prev_hash"] != expected_prev:
            return (False, row["id"])

        # ── 2. Own-hash recomputation ─────────────────────────────────────
        recomputed = _compute_hash(
            row["prev_hash"],
            row["agent_id"],
            row["tool_name"],
            row["decision"],
            row["reason"],
            row["est_cost"],
            row["actual_cost"],
            row["ts"],
        )
        if recomputed != row["row_hash"]:
            return (False, row["id"])

        expected_prev = row["row_hash"]

    return (True, None)
