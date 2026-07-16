"""
governance.py — Atomic spending-reservation and agent-control logic.

Public API
----------
reserve(pool, agent_id, tool_name, cost)              → bool
settle(pool, agent_id, reserved_amount, actual_cost)  → bool
refund(pool, agent_id, reserved_amount)               → bool   (convenience)
revoke(pool, agent_id)                                → bool
activate(pool, agent_id)                              → bool

All functions are intentionally pure: they take a pool as an argument so they
can be tested without a running FastAPI application.

Audit logging
-------------
reserve() and settle() call log_decision() after every decision so that every
allow/deny is written to the tamper-evident audit_log.  Logging happens AFTER
the decision is finalised and is wrapped in a bare try/except so that a logging
failure can never block or corrupt the actual financial operation.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import asyncpg

from app.audit import log_decision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# reserve()
# ---------------------------------------------------------------------------

async def reserve(
    pool: asyncpg.Pool,
    agent_id: str,
    tool_name: str,
    cost: Decimal,
) -> bool:
    """
    Atomically check permission, enforce the per-call limit, and reserve *cost*
    against an agent's spending cap — all in a single UPDATE statement.

    Returns True if the reservation succeeded.
    Returns False (never raises) if any of the following hold:
    • The agent does not exist or its status is not 'active'.
    • No permission row exists for (agent_id, tool_name) — tool not allowed.
    • A permission row exists but max_per_call is not NULL and cost > max_per_call.
    • Adding *cost* to (spent + reserved) would exceed cap.

    Why the permission check must live inside the same UPDATE:

    A two-step approach — first SELECT from agent_permissions to verify the
    tool is allowed, then UPDATE agents to increment reserved — has the same
    kind of race condition as a read-then-write budget check: between the
    SELECT and the UPDATE, another process could revoke the permission, change
    the per-call limit, or revoke the agent itself.  The caller's check would
    have passed on stale data, and the reservation would proceed even though
    the permission no longer exists.  By embedding a WHERE EXISTS subquery
    against agent_permissions directly inside the UPDATE's WHERE clause, the
    permission check and the budget increment happen atomically under the same
    row-level lock.  If the permission is revoked a microsecond before the
    UPDATE runs, the subquery finds no matching row, the WHERE clause is false,
    no row is updated, and RETURNING returns nothing — the call safely returns
    False.  There is no window between the check and the write.

    Every decision (allowed or denied) is appended to the audit_log after the
    fact.  Logging is best-effort: an error there never raises to the caller.
    """
    row = await pool.fetchrow(
        """
        UPDATE agents
           SET reserved = reserved + $3
         WHERE id      = $1
           AND status  = 'active'
           AND (spent + reserved + $3) <= cap
           AND EXISTS (
               SELECT 1
                 FROM agent_permissions ap
                WHERE ap.agent_id   = $1
                  AND ap.tool_name  = $2
                  AND (ap.max_per_call IS NULL OR $3 <= ap.max_per_call)
           )
        RETURNING id
        """,
        agent_id,
        tool_name,
        cost,
    )
    allowed = row is not None

    # ── Determine denial reason for the audit log ─────────────────────────
    if not allowed:
        reason = await _reserve_denial_reason(pool, agent_id, tool_name, cost)
        # We only log denials at reserve time. Successes are deferred to settle()
        # so that the actual cost and final completion status are recorded exactly once.
        await _fire_log(pool, agent_id, tool_name,
                  "denied", reason,
                  est_cost=cost, actual_cost=None)

    return allowed


async def _reserve_denial_reason(
    pool: asyncpg.Pool,
    agent_id: str,
    tool_name: str,
    cost: Decimal,
) -> str:
    """
    After a failed reserve(), run a cheap diagnostic SELECT to produce a
    human-readable reason for the audit log.

    This is NOT on the critical path — it only runs on the deny branch and the
    result is only used for logging, so the extra round-trip is acceptable.
    """
    row = await pool.fetchrow(
        "SELECT status, spent, reserved, cap FROM agents WHERE id = $1",
        agent_id,
    )
    if row is None:
        return "agent_not_found"
    if row["status"] != "active":
        return "revoked"
    if row["spent"] + row["reserved"] + cost > row["cap"]:
        return "cap_exceeded"

    perm = await pool.fetchrow(
        "SELECT max_per_call FROM agent_permissions WHERE agent_id=$1 AND tool_name=$2",
        agent_id, tool_name,
    )
    if perm is None:
        return "not_permitted"
    if perm["max_per_call"] is not None and cost > perm["max_per_call"]:
        return "per_call_limit_exceeded"

    return "denied_unknown"


# ---------------------------------------------------------------------------
# settle()
# ---------------------------------------------------------------------------

async def settle(
    pool: asyncpg.Pool,
    agent_id: str,
    reserved_amount: Decimal,
    actual_cost: Decimal,
    *,
    tool_name: str = "settle",
) -> bool:
    """
    Settle a completed tool call: release the reservation and record actual cost.

    In one atomic UPDATE (no separate SELECT):
    • Subtracts *reserved_amount* from the agent's ``reserved`` column.
    • Adds *actual_cost* to the agent's ``spent`` column.

    Returns True if the row was found and updated.
    Returns False if the agent does not exist, is not active, or ``reserved``
    would go negative (caller is trying to release more than was reserved —
    a logic error upstream).

    Partial-cost / refund semantics fall out naturally:
    • actual_cost < reserved_amount → the difference is freed back to headroom.
    • actual_cost == 0             → full refund; use refund() for clarity.

    Overrun behavior (hardened): if actual_cost > reserved_amount AND the
    resulting spent + reserved would exceed cap, Postgres raises
    ``asyncpg.exceptions.CheckViolationError`` with constraint name
    ``spent_reserved_within_cap``.  Callers should catch this error and treat
    it as a critical accounting anomaly, because it means a tool consumed more
    budget than was reserved for it.  Tools must reserve a conservative
    worst-case estimate to prevent this.

    The optional *tool_name* keyword argument is forwarded to the audit log so
    callers who know which tool they are settling can produce richer records.
    """
    row = await pool.fetchrow(
        """
        UPDATE agents
           SET reserved = reserved - $2,
               spent    = spent    + $3
         WHERE id      = $1
           AND status  = 'active'
           AND reserved >= $2
        RETURNING id
        """,
        agent_id,
        reserved_amount,
        actual_cost,
    )
    allowed = row is not None
    reason = "ok" if allowed else "settle_denied"

    await _fire_log(pool, agent_id, tool_name,
              "allowed" if allowed else "denied", reason,
              est_cost=reserved_amount, actual_cost=actual_cost)

    return allowed


# ---------------------------------------------------------------------------
# refund()
# ---------------------------------------------------------------------------

async def refund(
    pool: asyncpg.Pool,
    agent_id: str,
    reserved_amount: Decimal,
    *,
    tool_name: str = "refund",
) -> bool:
    """
    Convenience wrapper for a full tool-failure refund.

    Equivalent to settle(pool, agent_id, reserved_amount, actual_cost=Decimal('0')).
    Releases *reserved_amount* back to the agent's headroom and adds nothing to
    spent — as if the tool never ran.
    """
    return await settle(pool, agent_id, reserved_amount, Decimal("0"),
                        tool_name=tool_name)


# ---------------------------------------------------------------------------
# revoke() / activate()
# ---------------------------------------------------------------------------

async def revoke(pool: asyncpg.Pool, agent_id: str) -> bool:
    """
    Set the agent's status to 'revoked', immediately blocking all future
    reserve() calls for this agent.

    Returns True if the agent existed (and was updated), False if not found.
    The update is unconditional on current status so calling revoke() on an
    already-revoked agent is idempotent.
    """
    row = await pool.fetchrow(
        "UPDATE agents SET status = 'revoked' WHERE id = $1 RETURNING id",
        agent_id,
    )
    return row is not None


async def activate(pool: asyncpg.Pool, agent_id: str) -> bool:
    """
    Set the agent's status back to 'active', re-enabling reserve() calls.

    Returns True if the agent existed (and was updated), False if not found.
    Idempotent: calling activate() on an already-active agent is a no-op.
    """
    row = await pool.fetchrow(
        "UPDATE agents SET status = 'active' WHERE id = $1 RETURNING id",
        agent_id,
    )
    return row is not None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fire_log(
    pool: asyncpg.Pool,
    agent_id: str,
    tool_name: str,
    decision: str,
    reason: str,
    est_cost: Decimal | None,
    actual_cost: Decimal | None,
) -> None:
    """
    Await log_decision() and swallow any exception so that a logging failure
    can never propagate to — or corrupt — the financial result.

    We await directly (not fire-and-forget via ensure_future) so that the
    governance function returns only after the audit row is committed.  This
    guarantees that verify_chain() called immediately after reserve()/settle()
    will always see the row, and makes the system's state consistent from the
    caller's perspective: one call = one row in the log.
    """
    try:
        await log_decision(
            pool, agent_id, tool_name, decision, reason,
            est_cost, actual_cost,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("audit log_decision failed: %s", exc)

