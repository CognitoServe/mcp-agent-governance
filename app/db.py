"""
db.py — asyncpg connection-pool management and FastAPI dependency.

Usage in a route:
    async def my_route(conn: asyncpg.Connection = Depends(get_db)):
        row = await conn.fetchrow("SELECT ...")
"""

from __future__ import annotations

import asyncpg
from fastapi import Request

# Module-level pool reference; initialised by the lifespan in main.py.
_pool: asyncpg.Pool | None = None


async def init_db_pool() -> None:
    """Create the global asyncpg pool from the DATABASE_URL env var."""
    import os

    global _pool
    dsn = os.environ["DATABASE_URL"]  # set in .env / Docker env
    _pool = await asyncpg.create_pool(dsn=dsn)


async def close_db_pool() -> None:
    """Gracefully close the pool; called on application shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the active pool (raises if not initialised)."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialised.")
    return _pool


async def get_db(request: Request) -> asyncpg.Connection:  # noqa: ARG001
    """
    FastAPI dependency that yields a single connection from the pool.

    The connection is automatically released back to the pool when the
    request completes (or raises).
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn
