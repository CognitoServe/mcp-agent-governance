"""
scripts/mcp_demo.py — End-to-end MCP client demo.

Starts the governed MCP server as a child process via stdio transport,
then calls all three tools with four real test scenarios and prints
the exact JSON responses.

Run:
    $env:TEST_DATABASE_URL="postgresql://postgres@localhost:5432/test_db"
    python scripts/mcp_demo.py

The script is intentionally self-contained so it can be run without
any test framework.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Make sure the project root is on sys.path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER_CMD = [
    sys.executable,
    "-m", "app.mcp_server",
]

SERVER_ENV = {
    **os.environ,
    "DATABASE_URL": os.environ.get(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://postgres:changeme@localhost:5432/myapp",
        ),
    ),
}

SCENARIOS = [
    # (description, tool_name, kwargs)
    (
        "1. demo-agent-analyst calling search  -> SHOULD SUCCEED",
        "search",
        {"agent_id": "demo-agent-analyst", "query": "quarterly revenue trends"},
    ),
    (
        "2. demo-agent-analyst calling disburse -> SHOULD BE DENIED (no permission row)",
        "disburse",
        {"agent_id": "demo-agent-analyst", "amount": "10.00", "recipient": "acct-999"},
    ),
    (
        "3. demo-agent-finance calling disburse within cap -> SHOULD SUCCEED",
        "disburse",
        {"agent_id": "demo-agent-finance", "amount": "30.00", "recipient": "acct-888"},
    ),
    (
        "4. demo-agent-finance calling disburse OVER max_per_call (>$50) -> SHOULD BE DENIED",
        "disburse",
        {"agent_id": "demo-agent-finance", "amount": "75.00", "recipient": "acct-888"},
    ),
]


async def run_demo() -> None:
    params = StdioServerParameters(
        command=SERVER_CMD[0],
        args=SERVER_CMD[1:],
        env=SERVER_ENV,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # List available tools so we can confirm the server started cleanly
            tools_result = await session.list_tools()
            tool_names = [t.name for t in tools_result.tools]
            print(f"Server started. Available tools: {tool_names}\n")
            print("=" * 72)

            for description, tool_name, kwargs in SCENARIOS:
                print(f"\n{description}")
                print("-" * 72)

                result = await session.call_tool(tool_name, kwargs)

                # result.content is a list of ContentBlocks; the first is text
                raw_text = result.content[0].text if result.content else "{}"
                try:
                    parsed = json.loads(raw_text)
                    pretty = json.dumps(parsed, indent=2)
                except json.JSONDecodeError:
                    pretty = raw_text

                print(pretty)

            print("\n" + "=" * 72)
            print("Demo complete.")


if __name__ == "__main__":
    asyncio.run(run_demo())
