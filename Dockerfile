# ─────────────────────────────────────────────────────────────────────────────
# Multi-stage Dockerfile
#
# Stage 1 (builder): installs dependencies into a venv
# Stage 2 (runtime): copies the venv and app code only — no build tools
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: dependency builder ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps required to compile asyncpg's C extension
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated virtual environment
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Install Python deps (layer-cached unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 2: lean runtime image ───────────────────────────────────────────────
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy the pre-built venv from the builder stage
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# Copy application source
COPY app/ ./app/
COPY scripts/ ./scripts/

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8000

# uvicorn is started with --host 0.0.0.0 so it is reachable from outside the
# container.  Adjust --workers based on your instance size.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
