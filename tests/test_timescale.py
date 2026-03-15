import pytest
import psycopg
import httpx
import pandas as pd
from datetime import datetime, timezone, timedelta
import asyncio
import math
import uuid

pytestmark = pytest.mark.asyncio

async def test_timescale_analytical_functions(db_conn: psycopg.Connection, generate_symbol: str):
    """
    Test Phase 2 functionality: 
    - inserting 10k ticks (simulating the batch ingestor)
    - refreshing continuous aggregates
    - validating SQL fn_vwap against pandas baseline
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)
    base_price = 50000.0
    
    rows_to_insert = []
    
    # We will spread 10k ticks across 10 minutes so VWAP and OHLCV have data
    for i in range(10000):
        # Evenly spread over 600 seconds
        ts = now - timedelta(minutes=10) + timedelta(seconds=i * (600.0 / 10000.0))
        price = base_price + (i % 100)
        vol = 1.0 + (i % 5)
        rows_to_insert.append((ts, symbol, price, vol, 'B', str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN'))
        
    # Bulk insert using psycopg3 COPY
    with db_conn.cursor() as cur:
        with cur.copy("COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN") as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()
                
    # Refresh continuous aggregate
    with db_conn.cursor() as cur:
        # Materialized views like ohlcv_1m should exist from init.sql
        try:
            # We refresh the data spanning our insertions
            start_ts = now - timedelta(minutes=15)
            end_ts = now + timedelta(minutes=1)
            # The CALL must be executed independently of transaction blocks in some PG versions, 
            # but usually it auto-commits inside timescaledb or requires specific isolation.
            # We'll commit first just in case.
            cur.execute("CALL refresh_continuous_aggregate('ohlcv_1m', %s, %s);", (start_ts, end_ts))
            db_conn.commit()
        except psycopg.Error as e:
            # The testing environment might complain or it might be auto-refreshing. 
            # We print the error but continue to see if we can still query the standard views.
            print(f"Warning: Failed to refresh continuous aggregate: {e}")
            db_conn.rollback()
            
    # Calculate Pandas VWAP
    df = pd.DataFrame(rows_to_insert, columns=["ts", "symbol", "price", "volume", "side", "order_id", "trade_id", "exchange"])
    expected_vwap = (df["price"] * df["volume"]).sum() / df["volume"].sum()
    
    # Query SQL VWAP using fn_vwap
    with db_conn.cursor() as cur:
        start_ts = now - timedelta(minutes=15)
        end_ts = now + timedelta(minutes=1)
        cur.execute("SELECT fn_vwap(%s, %s, %s)", (symbol, start_ts, end_ts))
        row = cur.fetchone()
        assert row is not None
        db_vwap = row[0]
        
    # Validate correctness to 4 decimal places
    assert math.isclose(float(db_vwap), expected_vwap, rel_tol=1e-4), f"SQL VWAP {db_vwap} != Pandas VWAP {expected_vwap}"
    
    # Verify ohlcv_1m has rows
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ohlcv_1m WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        assert row is not None
        count = row[0]
        # At least 10 minutes covered -> 10 rows
        assert count >= 10, f"Expected >= 10 rows in ohlcv_1m, got {count}"
