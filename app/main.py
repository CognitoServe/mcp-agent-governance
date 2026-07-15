"""
main.py — FastAPI application entry point.

Registers the lifespan (DB pool open/close), mounts routers,
and exposes a health-check endpoint.  Add your own routers below
the TODO comment.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import close_db_pool, init_db_pool, get_pool
from app.errors import log_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the asyncpg pool on startup and close it on shutdown."""
    await init_db_pool()
    yield
    await close_db_pool()


app = FastAPI(
    title="My API",
    version="0.1.0",
    lifespan=lifespan,
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    try:
        pool = get_pool()
    except RuntimeError:
        pool = None

    if pool is not None:
        context = {
            "method": request.method,
            "url": str(request.url),
            "client": request.client.host if request.client else None
        }
        await log_error(pool, "fastapi.global", exc, context)
    
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )
    

# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", tags=["meta"])
async def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# TODO: mount your routers here
# e.g.  app.include_router(items.router, prefix="/items", tags=["items"])
# ---------------------------------------------------------------------------
