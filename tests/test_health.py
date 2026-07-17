"""
test_health.py — smoke test for the /health endpoint.

Run with:  pytest tests/test_health.py -v
"""

import os

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client():
    """
    Sync test client.

    DATABASE_URL must be set before the lifespan starts (it calls
    init_db_pool which reads that env var).  We point it at the test DB
    so this fixture is self-contained; the pool is opened and closed by
    the TestClient context manager via the FastAPI lifespan.
    """
    os.environ.setdefault(
        "DATABASE_URL",
        os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql://postgres:changeme@localhost:5432/myapp",
        ),
    )
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
