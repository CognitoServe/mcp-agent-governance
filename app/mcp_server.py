"""
app/mcp_server.py — Governed MCP tool server.

IMPORTANT: agent_id is an explicit required parameter on every tool here,
not inferred from context.  MCP has no native concept of caller identity for
billing or permissions: the protocol does not authenticate who is making the
tool call, so there is no built-in session token or principal that we could
use to derive the agent_id automatically.  In a real production deployment
this value would come from an authenticated session (e.g., a JWT claim or a
server-side mapping from the MCP client's TLS certificate), never from a
client-supplied string — a malicious or misconfigured client could trivially
impersonate any agent_id and bypass governance.  For this demonstration the
parameter is passed explicitly so the governance layer can be exercised, but
it must be treated as UNTRUSTED INPUT in any production context.

Usage
-----
Start the server (stdio transport, default):
    python -m app.mcp_server

Or via the MCP CLI:
    mcp run app/mcp_server.py

The server connects to the Postgres database identified by the DATABASE_URL
environment variable (falls back to the test DB if not set).
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import asyncpg
from mcp.server.fastmcp import FastMCP

from app.governance import reserve, settle
from app.governance import _reserve_denial_reason   # reuse the diagnostic SELECT
from app.errors import log_error

# ── Database connection ───────────────────────────────────────────────────────

_DSN = os.environ.get(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/test_db",
    ),
)

# The pool is initialised lazily on first use so the module can be imported
# without a live database (useful for testing imports).
_pool: asyncpg.Pool | None = None


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(dsn=_DSN, min_size=2, max_size=10)
    return _pool


# ── MCP server definition ─────────────────────────────────────────────────────

mcp = FastMCP(
    name="governed-agent-tools",
    instructions=(
        "Tools for governed agent actions. Every tool call is checked against "
        "the agent's permission set and spending cap before execution. "
        "Denied calls never touch the balance and return a structured denial "
        "with the specific reason."
    ),
)

# ── Shared helpers ─────────────────────────────────────────────────────────────

_TOOL_COSTS: dict[str, Decimal] = {
    "search":       Decimal("0.50"),
    "write_record": Decimal("2.00"),
    # disburse cost = amount itself, computed per-call
}


async def _denial_detail(pool: asyncpg.Pool, agent_id: str,
                          tool_name: str, cost: Decimal) -> str:
    """
    Return a human-readable denial sentence by calling the same diagnostic
    SELECT that governance.py already uses for audit-log reasons.
    """
    reason = await _reserve_denial_reason(pool, agent_id, tool_name, cost)
    messages = {
        "agent_not_found":        f"Agent '{agent_id}' does not exist.",
        "revoked":                f"Agent '{agent_id}' has been revoked and cannot take actions.",
        "cap_exceeded":           f"This call would exceed agent '{agent_id}'s spending cap.",
        "not_permitted":          f"Agent '{agent_id}' does not have permission to use '{tool_name}'.",
        "per_call_limit_exceeded": (
            f"The cost ${cost} exceeds the per-call limit for '{tool_name}' "
            f"on agent '{agent_id}'."
        ),
    }
    return messages.get(reason, f"Denied ({reason}).")


# ── Tool: search ──────────────────────────────────────────────────────────────

@mcp.tool()
async def search(agent_id: str, query: str) -> dict:
    """
    Perform a governed web search on behalf of an agent.

    Fixed cost: $0.50 per call.  Requires the 'search' permission in
    agent_permissions.  Returns the search results dict on success, or a
    structured denial dict if the call is not permitted.

    NOTE: agent_id must come from an authenticated session in production —
    see module docstring.
    """
    try:
        pool = await _get_pool()
        cost = _TOOL_COSTS["search"]

        allowed = await reserve(pool, agent_id, "search", cost)
        if not allowed:
            detail = await _denial_detail(pool, agent_id, "search", cost)
            return {
                "status":  "denied",
                "tool":    "search",
                "agent":   agent_id,
                "reason":  detail,
            }

        # ── Simulated action ──────────────────────────────────────────────────────
        result_payload = {
            "status":  "ok",
            "tool":    "search",
            "agent":   agent_id,
            "query":   query,
            "results": [
                {"title": "Result 1 for: " + query, "url": "https://example.com/1"},
                {"title": "Result 2 for: " + query, "url": "https://example.com/2"},
            ],
            "cost_charged": str(cost),
        }

        await settle(pool, agent_id, cost, cost, tool_name="search")
        return result_payload
    except Exception as exc:
        pool = await _get_pool()
        await log_error(pool, "mcp_server.search", exc, {"agent_id": agent_id, "query": query})
        raise


# ── Tool: write_record ────────────────────────────────────────────────────────

@mcp.tool()
async def write_record(agent_id: str, record: str) -> dict:
    """
    Write a record to the governed data store on behalf of an agent.

    Fixed cost: $2.00 per call.  Requires the 'write_record' permission.
    Returns the written record's confirmation on success, or a structured
    denial dict if the call is not permitted.

    NOTE: agent_id must come from an authenticated session in production —
    see module docstring.
    """
    try:
        pool = await _get_pool()
        cost = _TOOL_COSTS["write_record"]

        allowed = await reserve(pool, agent_id, "write_record", cost)
        if not allowed:
            detail = await _denial_detail(pool, agent_id, "write_record", cost)
            return {
                "status": "denied",
                "tool":   "write_record",
                "agent":  agent_id,
                "reason": detail,
            }

        # ── Simulated action ──────────────────────────────────────────────────────
        import hashlib, datetime
        record_id = hashlib.sha1(
            f"{agent_id}:{record}:{datetime.datetime.utcnow().isoformat()}".encode()
        ).hexdigest()[:12]

        result_payload = {
            "status":       "ok",
            "tool":         "write_record",
            "agent":        agent_id,
            "record_id":    record_id,
            "bytes_written": len(record.encode()),
            "cost_charged": str(cost),
        }

        await settle(pool, agent_id, cost, cost, tool_name="write_record")
        return result_payload
    except Exception as exc:
        pool = await _get_pool()
        await log_error(pool, "mcp_server.write_record", exc, {"agent_id": agent_id})
        raise


# ── Tool: disburse ────────────────────────────────────────────────────────────

@mcp.tool()
async def disburse(agent_id: str, amount: str, recipient: str) -> dict:
    """
    Disburse funds to a recipient on behalf of an agent.

    Cost = amount itself (the disbursement IS the cost).  This is the
    highest-privilege, most-restricted tool: agents need explicit 'disburse'
    permission AND the amount must not exceed their max_per_call limit.

    amount is passed as a string to avoid float precision issues; it is
    parsed into Decimal internally.  Example: "25.00".

    NOTE: agent_id must come from an authenticated session in production —
    see module docstring.
    """
    try:
        pool = await _get_pool()

        try:
            cost = Decimal(amount)
        except Exception:
            return {
                "status": "error",
                "tool":   "disburse",
                "agent":  agent_id,
                "reason": f"Invalid amount '{amount}': must be a decimal number string.",
            }

        if cost <= Decimal("0"):
            return {
                "status": "error",
                "tool":   "disburse",
                "agent":  agent_id,
                "reason": "Amount must be greater than zero.",
            }

        allowed = await reserve(pool, agent_id, "disburse", cost)
        if not allowed:
            detail = await _denial_detail(pool, agent_id, "disburse", cost)
            return {
                "status":  "denied",
                "tool":    "disburse",
                "agent":   agent_id,
                "amount":  amount,
                "reason":  detail,
            }

        # ── Simulated disbursement ────────────────────────────────────────────────
        import uuid
        tx_id = str(uuid.uuid4())

        result_payload = {
            "status":      "ok",
            "tool":        "disburse",
            "agent":       agent_id,
            "tx_id":       tx_id,
            "amount":      amount,
            "recipient":   recipient,
            "cost_charged": amount,
        }

        await settle(pool, agent_id, cost, cost, tool_name="disburse")
        return result_payload
    except Exception as exc:
        pool = await _get_pool()
        await log_error(pool, "mcp_server.disburse", exc, {"agent_id": agent_id, "amount": amount, "recipient": recipient})
        raise


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
