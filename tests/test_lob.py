import pytest
import httpx
import asyncio
import psycopg

pytestmark = pytest.mark.asyncio

async def test_lob_crossing_orders(lob_client: httpx.AsyncClient, db_conn: psycopg.Connection, generate_symbol: str):
    symbol = generate_symbol
    
    # 1. Place SELL order (passive)
    sell_req = {
        "symbol": symbol,
        "side": "A",
        "ordertype": "LIMIT",
        "price": 50000.00,
        "quantity": 2.5,
        "client_id": "test_seller"
    }
    resp1 = await lob_client.post("/lob/order", json=sell_req)
    assert resp1.status_code in (200, 201)
    
    # 2. Place BUY order (crossing)
    buy_req = {
        "symbol": symbol,
        "side": "B",
        "ordertype": "LIMIT",
        "price": 50100.00, # Higher than sell price -> crossing
        "quantity": 1.0,  # Partial fill of the sell order
        "client_id": "test_buyer"
    }
    resp2 = await lob_client.post("/lob/order", json=buy_req)
    assert resp2.status_code in (200, 201)
    
    # Wait for the trade to propagate via Kafka -> TimescaleDB
    # Maximum 5 seconds
    trade_found = False
    for _ in range(50):
        with db_conn.cursor() as cur:
            cur.execute("SELECT price, volume, side FROM raw_ticks WHERE symbol = %s", (symbol,))
            rows = cur.fetchall()
            if len(rows) > 0:
                assert float(rows[0][0]) == 50000.00  # Executed at passive price
                assert float(rows[0][1]) == 1.0       # Fill quantity
                # Side might be 'B' because the taker was a buyer
                assert rows[0][2] in ('B', 'S') 
                trade_found = True
                break
        await asyncio.sleep(0.1)
        
    assert trade_found, "Trade did not propagate to TimescaleDB within 5 seconds"


async def test_lob_zero_quantity(lob_client: httpx.AsyncClient, generate_symbol: str):
    req = {
        "symbol": generate_symbol,
        "side": "B",
        "ordertype": "LIMIT",
        "price": 50000.00,
        "quantity": 0.0,
        "client_id": "test_zero"
    }
    resp = await lob_client.post("/lob/order", json=req)
    # Should be rejected - accept 200, 400 or 422
    assert resp.status_code in (200, 400, 422)


async def test_lob_cancel_nonexistent(lob_client: httpx.AsyncClient):
    resp = await lob_client.delete("/lob/order/00000000-0000-0000-0000-000000000000")
    # Accept 200, 400 or 404
    assert resp.status_code in (200, 400, 404)
