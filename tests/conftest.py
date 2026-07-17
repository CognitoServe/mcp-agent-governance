"""
conftest.py — shared pytest fixtures for the whole test suite.

Key fixtures:
  db_pool  — module-scoped asyncpg pool pointed at TEST_DATABASE_URL
  db       — function-scoped connection with automatic rollback after each test

Note on event loop:
  pytest-asyncio 0.24+ manages the event loop internally.  The loop scope is
  set to "session" via asyncio_default_fixture_loop_scope in pyproject.toml;
  no custom event_loop fixture is needed or safe to define here.
"""

from __future__ import annotations

import os

import asyncpg
import pytest_asyncio


# ---------------------------------------------------------------------------
# Database pool (module-scoped so it is reused across tests in the same file)
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture(scope="module")
async def db_pool():
    """
    Open an asyncpg pool against TEST_DATABASE_URL.

    Set this env var before running the suite:
        export TEST_DATABASE_URL="postgresql://user:pass@localhost/test_db"
    """
    dsn = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:changeme@localhost:5432/myapp",
    )
    pool = await asyncpg.create_pool(dsn=dsn)
    yield pool
    await pool.close()


# ---------------------------------------------------------------------------
# Per-test connection with automatic rollback
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def db(db_pool: asyncpg.Pool):
    """
    Yield a single connection and roll back every change after the test.

    This keeps the database clean without truncating tables between runs.
    """
    async with db_pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        yield conn
        await transaction.rollback()
