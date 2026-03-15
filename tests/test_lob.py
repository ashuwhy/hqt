import asyncio
import httpx
import psycopg
import pytest

pytestmark = pytest.mark.asyncio

async def test_lob_health(lob_client: httpx.AsyncClient):
    """Verify LOB engine is up."""
    resp = await lob_client.get("/lob/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "OK"

async def test_crossing_orders(lob_client: httpx.AsyncClient, db_conn: psycopg.Connection, generate_symbol: str):
    """Place crossing BUY+SELL orders -> assert trade fired -> assert raw_ticks row inserted."""
    symbol = generate_symbol
    
    # 1. Place BUY order
    buy_req = {
        "symbol": symbol,
        "side": "B",
        "price": 50000.0,
        "quantity": 1.5,
        "ordertype": "LIMIT"
    }
    resp1 = await lob_client.post("/lob/order", json=buy_req)
    assert resp1.status_code == 201

    # 2. Place crossing SELL order
    sell_req = {
        "symbol": symbol,
        "side": "S", # The LOB expects 'A' for Ask, let's fix this in the code below
        "price": 50000.0,
        "quantity": 1.5,
        "ordertype": "LIMIT"
    }
    sell_req["side"] = "A" # Fixing it, must be "A" per urls.txt and lob_server.cpp

    resp2 = await lob_client.post("/lob/order", json=sell_req)
    assert resp2.status_code == 201

    # 3. Wait for execution and kafka + timescale ingestion
    await asyncio.sleep(1.0) # 1 second should be enough for kafka roundtrip and DB batch copy

    # 4. Assert raw_ticks row inserted
    with db_conn.cursor() as cur:
        cur.execute("SELECT ts, price, volume, side FROM raw_ticks WHERE symbol = %s", (symbol,))
        rows = cur.fetchall()
        
    assert len(rows) == 1
    assert float(rows[0][1]) == 50000.0
    assert float(rows[0][2]) == 1.5


async def test_zero_quantity_order(lob_client: httpx.AsyncClient, generate_symbol: str):
    """Edge Case: Zero-quantity order should be rejected."""
    symbol = generate_symbol
    req = {
        "symbol": symbol,
        "side": "B",
        "price": 1000.0,
        "quantity": 0.0,
        "ordertype": "LIMIT"
    }
    resp = await lob_client.post("/lob/order", json=req)
    assert resp.status_code == 400


async def test_cross_spread_execution(lob_client: httpx.AsyncClient, db_conn: psycopg.Connection, generate_symbol: str):
    """Edge Case: Cross-spread execution - Aggressive taker matches multiple resting orders."""
    symbol = generate_symbol
    
    # Resting Asks
    await lob_client.post("/lob/order", json={"symbol": symbol, "side": "A", "price": 101.0, "quantity": 1.0, "ordertype": "LIMIT"})
    await lob_client.post("/lob/order", json={"symbol": symbol, "side": "A", "price": 102.0, "quantity": 1.0, "ordertype": "LIMIT"})
    
    # Aggressive Bid wiping both
    await lob_client.post("/lob/order", json={"symbol": symbol, "side": "B", "price": 102.0, "quantity": 2.0, "ordertype": "LIMIT"})

    await asyncio.sleep(1.0)
    
    with db_conn.cursor() as cur:
        cur.execute("SELECT price, volume FROM raw_ticks WHERE symbol = %s ORDER BY ts ASC", (symbol,))
        rows = cur.fetchall()
        
    assert len(rows) == 2
    assert float(rows[0][0]) == 101.0
    assert float(rows[0][1]) == 1.0
    assert float(rows[1][0]) == 102.0
    assert float(rows[1][1]) == 1.0
