"""
tests.test_integration_e2e
~~~~~~~~~~~~~~~~~~~~~~~~~~
End-to-end cross-module integration tests requiring the full Docker stack.

These tests verify data flows across module boundaries:
  - LOB → Kafka → TimescaleDB
  - Security Proxy → upstream services (graph, quantum, analytics)
  - Full arbitrage signal detection pipeline

Run with:
    docker compose exec data-ingestor python -m pytest tests/test_integration_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os

import httpx
import psycopg
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def proxy_client() -> httpx.AsyncClient:
    """HTTP client targeting the security proxy (port 8000)."""
    base_url = os.getenv("PROXY_TEST_URL", "http://fastapi-proxy:8000")
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        yield client


# ── Cross-module E2E tests ────────────────────────────────────────────────────

async def test_e2e_lob_to_timescale_pipeline(
    lob_client: httpx.AsyncClient,
    db_conn: psycopg.Connection,
    generate_symbol: str,
):
    """
    Full pipeline: LOB crossing orders → Kafka → TimescaleDB.

    Steps:
      1. Place a passive ASK order on the LOB
      2. Place a crossing BID order (triggers a trade)
      3. Wait for the trade to propagate through Kafka into raw_ticks
      4. Assert the trade row exists in TimescaleDB
    """
    symbol = generate_symbol

    # Step 1: passive sell
    sell = {
        "symbol": symbol, "side": "A", "ordertype": "LIMIT",
        "price": 45000.00, "quantity": 3.0, "client_id": "e2e_seller"
    }
    r1 = await lob_client.post("/lob/order", json=sell)
    assert r1.status_code in (200, 201), f"Failed to place sell: {r1.status_code} {r1.text}"

    # Step 2: crossing buy
    buy = {
        "symbol": symbol, "side": "B", "ordertype": "LIMIT",
        "price": 45100.00, "quantity": 1.5, "client_id": "e2e_buyer"
    }
    r2 = await lob_client.post("/lob/order", json=buy)
    assert r2.status_code in (200, 201), f"Failed to place buy: {r2.status_code} {r2.text}"

    # Step 3: poll raw_ticks for up to 10 seconds
    found = False
    for _ in range(100):
        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT price, volume FROM raw_ticks WHERE symbol = %s ORDER BY ts DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
            if row:
                assert float(row[0]) == 45000.00, f"Trade price mismatch: {row[0]}"
                assert float(row[1]) == 1.5, f"Trade volume mismatch: {row[1]}"
                found = True
                break
        await asyncio.sleep(0.1)

    assert found, f"Trade for {symbol} did not propagate to TimescaleDB within 10s"


async def test_e2e_graph_health_from_proxy(proxy_client: httpx.AsyncClient):
    """Security proxy → graph-service /graph/health must return 200."""
    resp = await proxy_client.get("/graph/health")
    if resp.status_code == 502:
        pytest.skip("graph-service not reachable through proxy — skipping")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("status") == "ok"


async def test_e2e_quantum_health_from_proxy(proxy_client: httpx.AsyncClient):
    """Security proxy → quantum-engine /quantum/health must return 200."""
    resp = await proxy_client.get("/quantum/health")
    assert resp.status_code == 200, (
        f"Quantum health through proxy returned {resp.status_code}: {resp.text}\n"
        f"quantum_api.py registers both /health and /quantum/health — "
        f"check that fastapi-proxy routes /quantum/* to quantum-engine:8004"
    )
    body = resp.json()
    assert body.get("status") == "ok"


async def test_e2e_analytics_health_from_proxy(proxy_client: httpx.AsyncClient):
    """Security proxy → data-ingestor /analytics/health must return 200."""
    resp = await proxy_client.get("/analytics/health")
    assert resp.status_code == 200, (
        f"Analytics health through proxy returned {resp.status_code}: {resp.text}\n"
        f"Check that module5_security/main.py routes /analytics/* to data-ingestor:8002"
    )


async def test_e2e_full_arbitrage_signal_flow(db_conn: psycopg.Connection):
    """
    Verify that arbitrage_signals table exists and is queryable.
    If the Bellman-Ford detector has been running, there should be ≥1 CLASSICAL signals.
    """
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*), MAX(ts) FROM arbitrage_signals
            WHERE method = 'CLASSICAL'
            """
        )
        row = cur.fetchone()

    assert row is not None, "arbitrage_signals query returned no row"
    count = int(row[0])
    # The table must have at least one CLASSICAL signal — the detector runs every 500ms
    assert count > 0, (
        f"No CLASSICAL signals in arbitrage_signals after waiting — "
        f"Bellman-Ford detector may not be running (count={count})"
    )


async def test_e2e_proxy_sql_injection_protection(proxy_client: httpx.AsyncClient):
    """
    Full stack: security proxy should block SQL injection before it reaches any backend.
    """
    resp = await proxy_client.post(
        "/lob/order",
        content='{"symbol": "BTC/USD\'; DROP TABLE raw_ticks;--", "side": "B", "price": 1, "quantity": 1}',
        headers={"Content-Type": "application/json"},
    )
    # Should be blocked by the SQL firewall (403) before reaching LOB
    assert resp.status_code == 403, f"SQL injection should be blocked, got {resp.status_code}"
