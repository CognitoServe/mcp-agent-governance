# Agent Spending Governor

[![CI](https://github.com/CognitoServe/mcp-agent-governance/actions/workflows/ci.yml/badge.svg)](https://github.com/CognitoServe/mcp-agent-governance/actions/workflows/ci.yml)

The Agent Spending Governor is an intelligent proxy that places immutable, cryptographic constraints on autonomous AI agent spending and tool execution. By moving governance enforcement out of the LLM prompt and into a transactional Postgres database using a hash-chained ledger, the system guarantees that compromised, hallucinating, or malicious agents cannot exceed their financial caps or escalate their granted permissions, regardless of the inputs they receive.

## Verified

Tests pass consistently on both Windows and Linux environments from a fresh virtual environment requiring zero manual steps beyond `pip install -r requirements.txt`. The adversarial test suite runs continuously via GitHub Actions, verifying 8 specific threat categories against a live Postgres instance: Concurrent cap-breach, Injection-style arguments, Permission escalation, Revoked-agent replay, Revoke-vs-reserve race, Negative-cost injection, Audit deletion detection, and Malformed input. The raw evidence of the adversarial suite blocking these behaviors can be viewed directly in the Actions logs.

## Architecture

1. **Agent Context**: The autonomous agent decides it needs to execute a tool.
2. **MCP**: The agent attempts to call the tool via the Model Context Protocol.
3. **Governance Check (Reserve)**: Before execution, the system intercepts the call, validates permissions, and reserves the maximum potential cost of the tool within the Postgres database.
4. **Execute**: The underlying tool executes in reality.
5. **Settle & Audit**: The exact final cost is settled, the reservation is cleared, and an immutable, hash-chained audit log entry is written containing the cryptographic footprint of the transaction.
6. **Response**: The result is returned to the agent.

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

## Bugs I actually hit building this

- **pytest-asyncio event loop scoping**: Database tests were failing with `attached to a different loop` errors because `pytest-asyncio` by default creates a new event loop per test, while our asyncpg connection pool was bound to the module-scoped loop. The fix was explicitly overriding the `event_loop` fixture in `conftest.py` to yield a single module-scoped event loop.
- **Decimal hash mismatches**: Cryptographic hashes were failing verification because `Decimal('0')` and `Decimal('0.00')` produce different string representations during JSON serialization. The fix involved normalizing all decimal values to exactly two decimal places in the Python layer prior to hashing.
- **Identical timestamp concurrency**: During high-concurrency race condition tests, multiple audit log entries were being written in the exact same millisecond, breaking the strictly chronological `ORDER BY timestamp` requirement of the hash chain. The fix was altering the order logic to fall back to `ORDER BY timestamp ASC, id ASC`.
- **pywin32 unconditional pin**: The `requirements.txt` file contained an unconditional pin for `pywin32==312`, which completely broke fresh `pip install -r requirements.txt` execution on Linux/Mac as pip evaluates the entire graph before installing. The fix was appending the environment marker `; sys_platform == 'win32'` to Windows-specific dependencies.

## License

This project is licensed under the MIT License.
