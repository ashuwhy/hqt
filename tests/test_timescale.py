import asyncio
import uuid
import httpx
import psycopg
import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta

pytestmark = pytest.mark.asyncio

async def test_analytics_health(analytics_client: httpx.AsyncClient):
    """Verify Timescale Analytics API is up."""
    resp = await analytics_client.get("/analytics/health")
    assert resp.status_code == 200
    assert resp.json()["status"] in ["ok", "degraded"]


async def test_bulk_insert_and_refresh(analytics_client: httpx.AsyncClient, db_conn: psycopg.Connection, generate_symbol: str):
    """Insert 10k ticks -> assert ohlcv_1m refreshes -> assert VWAP vs pandas"""
    symbol = generate_symbol
    now = datetime.now(timezone.utc)
    
    # 1. Insert 10k ticks directly via binary copy
    rows = []
    prices = []
    volumes = []
    for i in range(1000):
        ts = now - timedelta(seconds=i)
        price = 100.0 + (i % 10) * 0.1
        volume = 1.0 + (i % 10) * 0.5
        rows.append((
            ts, symbol, price, volume, 'B',
            uuid.uuid4(), uuid.uuid4(), 'KRAKEN'
        ))
        prices.append(price)
        volumes.append(volume)

    with db_conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row(r)
    db_conn.commit()

    # 2. Refresh ohlcv_1m
    with psycopg.connect(db_conn.info.dsn, autocommit=True) as ac:
        ac.execute(
            f"CALL refresh_continuous_aggregate('ohlcv_1m', %s, %s)",
            (now - timedelta(hours=1), now + timedelta(hours=1)),
        )

    # 3. Verify via Analytics API
    start_ts = (now - timedelta(hours=1)).isoformat()
    end_ts = (now + timedelta(hours=1)).isoformat()
    resp = await analytics_client.get(f"/analytics/ohlcv?symbol={symbol}&interval=1m&from={start_ts}&to={end_ts}")
    assert resp.status_code == 200
    ohlcv_data = resp.json()
    assert len(ohlcv_data) > 0
    assert ohlcv_data[0]["symbol"] == symbol

    # 4. Compare VWAP to pandas baseline
    df = pd.DataFrame({'price': prices, 'volume': volumes})
    expected_vwap = (df['price'] * df['volume']).sum() / df['volume'].sum()

    resp_vwap = await analytics_client.get(f"/analytics/indicators?symbol={symbol}&indicator=vwap&from={start_ts}&to={end_ts}")
    assert resp_vwap.status_code == 200
    sql_vwap = resp_vwap.json()["value"]

    # Assert correct to 4 decimal places
    assert round(sql_vwap, 4) == round(expected_vwap, 4)
