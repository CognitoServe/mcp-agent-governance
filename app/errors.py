"""
app/errors.py — Technical error logging.
"""

import json
import logging

import asyncpg

logger = logging.getLogger(__name__)

async def log_error(pool: asyncpg.Pool, source: str, exc: Exception, context: dict | None = None) -> None:
    """
    Log an unexpected technical exception to the error_log table.

    This function never raises an exception itself. If writing to the database
    fails, it falls back to standard stderr logging so the caller's error handling
    is not disrupted by a broken error logger.
    """
    try:
        error_type = exc.__class__.__name__
        message = str(exc)
        context_json = json.dumps(context) if context else None

        await pool.execute(
            """
            INSERT INTO error_log (source, error_type, message, context)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            source, error_type, message, context_json
        )
    except Exception as log_exc:
        logger.error(
            "Failed to write to error_log: %s. Original error [%s] from %s: %s",
            log_exc, exc.__class__.__name__, source, exc
        )
