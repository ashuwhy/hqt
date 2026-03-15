import asyncio
import os
import uuid
from typing import AsyncGenerator

import httpx
import psycopg
import pytest

# Test constants
LOB_URL = os.getenv("LOB_ENGINE_URL", "http://localhost:8001")
ANALYTICS_URL = os.getenv("ANALYTICS_URL", "http://localhost:8002")
PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def lob_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async client for Phase 1 LOB engine."""
    async with httpx.AsyncClient(base_url=LOB_URL) as client:
        yield client


@pytest.fixture(scope="session")
async def analytics_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async client for Phase 2 Analytics API."""
    async with httpx.AsyncClient(base_url=ANALYTICS_URL) as client:
        yield client


@pytest.fixture(scope="function")
def db_conn():
    """Provides a fresh database connection for each test function."""
    conn = psycopg.connect(PG_DSN, autocommit=True)
    yield conn
    conn.close()


@pytest.fixture
def generate_symbol():
    """Generate a unique symbol for isolation."""
    return f"TEST-{uuid.uuid4().hex[:6]}"
