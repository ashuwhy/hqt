import pytest
import psycopg
import httpx
import random
import string
import os
from typing import AsyncGenerator, Generator


@pytest.fixture(scope="session")
def db_conn() -> Generator[psycopg.Connection, None, None]:
    """Provide a synchronous psycopg3 connection to TimescaleDB for testing."""
    # Since tests run inside docker (e.g. data-ingestor), 'postgres' is the hostname
    # We allow overriding with env vars if running locally on host
    host = os.getenv("PGHOST", "postgres")
    port = os.getenv("PGPORT", "5432")
    user = os.getenv("PGUSER", "hqt")
    password = os.getenv("PGPASSWORD", "hqt_secret")
    dbname = os.getenv("PGDATABASE", "hqt")

    conn_str = f"host={host} port={port} dbname={dbname} user={user} password={password}"
    conn = psycopg.connect(conn_str, autocommit=True)
    
    yield conn
    
    conn.close()


@pytest.fixture
def generate_symbol() -> str:
    """Generate a unique test symbol to avoid collisions with live data."""
    # E.g. TEST-A1B2C3D4-USD
    rand_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    return f"TEST-{rand_suffix}-USD"


@pytest.fixture
async def lob_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for the LOB engine API."""
    # LOB engine runs on port 8001; if testing inside data-ingestor we can hit lob-engine:8001
    base_url = os.getenv("LOB_TEST_URL", "http://lob-engine:8001")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        yield client


@pytest.fixture
async def analytics_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTP client for the Analytics API."""
    # Analytics API runs on port 8002; since tests may run INSIDE data-ingestor container, it can be localhost:8002
    base_url = os.getenv("ANALYTICS_TEST_URL", "http://localhost:8002")
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        yield client
