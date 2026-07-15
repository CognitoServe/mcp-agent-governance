"""
tests/test_errors.py — Tests for technical error logging.
"""

import asyncio
import os
import uuid
from decimal import Decimal

import asyncpg
import pytest

from app.mcp_server import search, _get_pool
from app.errors import log_error

@pytest.mark.asyncio
async def test_error_log_captures_unexpected_exception():
    """
    Deliberately pass an invalid type (None) to an MCP tool to trigger an unexpected
    exception (asyncpg DataError or InterfaceError). The tool should catch it,
    log it to error_log, and re-raise.
    """
    pool = await _get_pool()
    # 1. Clear error_log just for a clean slate
    await pool.execute("DELETE FROM error_log")

    # 2. Call the tool with a valid agent but query=None to trigger TypeError
    #    during string concatenation in the simulated action.
    with pytest.raises(Exception) as exc_info:
        await search(agent_id="demo-agent-analyst", query=None)

    # 3. Assert the exception was actually raised
    assert exc_info.value is not None

    # 4. Check error_log for the captured exception
    rows = await pool.fetch("SELECT source, error_type, message, context FROM error_log")
    
    assert len(rows) == 1, "Expected exactly 1 row in error_log"
    row = rows[0]
    
    assert row["source"] == "mcp_server.search"
    assert "error" in row["error_type"].lower() or "exception" in row["error_type"].lower()
    assert row["error_type"] == exc_info.value.__class__.__name__
    
    # context should contain the passed arguments
    import json
    ctx = json.loads(row["context"])
    assert ctx["agent_id"] == "demo-agent-analyst"
    assert ctx["query"] is None
