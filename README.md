# Agent Spending Governor

[![CI](https://github.com/CognitoServe/mcp-agent-governance/actions/workflows/ci.yml/badge.svg)](https://github.com/CognitoServe/mcp-agent-governance/actions/workflows/ci.yml)

The Agent Spending Governor is an intelligent proxy that places immutable, cryptographic constraints on autonomous AI agent spending and tool execution. By moving governance enforcement out of the LLM prompt and into a transactional Postgres database using a hash-chained ledger, the system guarantees that compromised, hallucinating, or malicious agents cannot exceed their financial caps or escalate their granted permissions, regardless of the inputs they receive.

## Verified

Tests pass consistently on both Windows and Linux environments from a fresh virtual environment requiring zero manual steps beyond `pip install -r requirements.txt`. The adversarial test suite runs continuously via GitHub Actions, verifying 8 specific threat categories against a live Postgres instance: Concurrent cap-breach, Injection-style arguments, Permission escalation, Revoked-agent replay, Revoke-vs-reserve race, Negative-cost injection, Audit deletion detection, and Malformed input. The raw evidence of the adversarial suite blocking these behaviors can be viewed directly in the Actions logs.

Below is a screenshot of the mission control dashboard showing the live system status and the completed security scorecard with all 8 adversarial threat vectors successfully mitigated:

![Security Scorecard](docs/images/dashboard_scorecard.png)

## Architecture

1. **Agent Context**: The autonomous agent decides it needs to execute a tool.
2. **MCP**: The agent attempts to call the tool via the Model Context Protocol.
3. **Governance Check (Reserve)**: Before execution, the system intercepts the call, validates permissions, and reserves the maximum potential cost of the tool within the Postgres database.
4. **Execute**: The underlying tool executes in reality.
5. **Settle & Audit**: The exact final cost is settled, the reservation is cleared, and an immutable, hash-chained audit log entry is written containing the cryptographic footprint of the transaction.
6. **Response**: The result is returned to the agent.
## Bugs I actually hit building this

1. **pytest-asyncio event-loop-per-test scoping**: An asyncpg pool created in a fixture bound to a different event loop than the test function, causing "attached to a different loop" errors. This was fixed by creating the pool inside the test body so it always shares the test's own loop.

2. **Decimal('0') vs Decimal('0.00') hash mismatch**: The audit hash was computed from a locally-constructed Decimal('0') on write, but Postgres always returns NUMERIC(12,2) values with 2 decimal places on read, causing the same value to hash differently depending on whether it originated from Python or the database. This was fixed by canonicalizing every value to 2 decimal places before hashing, on both write and verify.

3. **Identical timestamps in concurrent audit writes**: The timestamp `ts` was captured before acquiring the per-agent advisory lock, causing multiple concurrent tasks in `asyncio.gather` to grab the same microsecond value before reaching the database. This was fixed by capturing `ts` after the lock is acquired, ensuring it reflects when each write actually got its turn.

4. **pywin32 breaking Linux installs**: The `requirements.txt` file was generated via a raw `pip freeze` on a Windows development machine, which unconditionally captured Windows-specific packages. Since pip resolves the full dependency graph before installation, this unsatisfiable platform-specific pin caused the entire installation to fail on Linux. This was fixed by adding environment markers (`; sys_platform == 'win32'`).

5. **Audit log gap on tool failure**: If a tool call succeeded past `reserve()` but failed during execution before `settle()` ran, the reservation would leak permanently against the agent's cap with zero audit trail. This was fixed by wrapping the execute step in its own `try/except` block that calls `refund()` on failure, ensuring the reservation is released and exactly one audit row is written.

## Quickstart

### Docker Compose
To run the system in an isolated environment with Postgres pre-configured:
```bash
docker compose up --build -d
```
The API and read-only dashboard will be available at `http://localhost:8000`.

### Local Installation (Pip)
To run natively:
```bash
# 1. Start a local Postgres instance
# 2. Setup your virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. Apply the database schema
psql -h localhost -U postgres -d myapp -f app/schema.sql

# 4. Start the server
export DATABASE_URL=postgresql://postgres:changeme@localhost:5432/myapp
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## License

This project is licensed under the MIT License.
