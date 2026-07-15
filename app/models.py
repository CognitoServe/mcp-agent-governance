"""
models.py — Pydantic schemas / domain models.

Define your request/response schemas and any domain value-objects here.
asyncpg returns plain dicts/Records, so Pydantic models live in this file
rather than being tied to an ORM.

Example:
    class Item(BaseModel):
        id: int
        name: str
        description: str | None = None
"""

from pydantic import BaseModel  # noqa: F401 — re-export for convenience

# ---------------------------------------------------------------------------
# TODO: add your models below
# ---------------------------------------------------------------------------
