import asyncio
import os
import pytest
from datetime import datetime, timedelta, timezone
from module2_timescale.smart_backfiller import detect_gaps
from module2_timescale.kafka_consumer import _parse_message
import json
import psycopg

pytestmark = pytest.mark.asyncio


def test_kafka_parser():
    """Verify Kafka consumer parses LOB JSON correctly to raw_ticks dict."""
    ts_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    raw = json.dumps({
        "ts": ts_ns,
        "symbol": "BTC/USD",
        "price": 65000.5,
        "qty": 2.0,
        "liquidity_side": "Ask",
        "passive_id": 100,
        "taker_id": 101,
    }).encode()
    
    parsed = _parse_message(raw)
    assert parsed is not None
    assert parsed["symbol"] == "BTC/USD"
    assert parsed["price"] == 65000.5
    assert parsed["volume"] == 2.0
    assert parsed["side"] == "S"  # Ask -> Sell


async def test_gap_detection(db_conn: psycopg.Connection, generate_symbol: str):
    """Insert ticks with explicit gaps and test smart_backfiller detect_gaps()."""
    symbol = generate_symbol
    now = datetime.now(timezone.utc)
    
    try:
        # Create a gap: Tick 1 at now - 15 minutes, Tick 2 at now - 1 minute.
        # This is a 14 minute gap -> should instantly be detected since GAP_THRESHOLD_S = 120
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange)
                VALUES (%s, %s, %s, %s, %s, gen_random_uuid(), gen_random_uuid(), 'KRAKEN')
            """, (now - timedelta(minutes=15), symbol, 50000.0, 1.0, 'B'))
            
            cur.execute("""
                INSERT INTO raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange)
                VALUES (%s, %s, %s, %s, %s, gen_random_uuid(), gen_random_uuid(), 'KRAKEN')
            """, (now - timedelta(minutes=1), symbol, 50100.0, 1.0, 'B'))
            
        gaps = detect_gaps(db_conn, lookback_hours=1) # 1 hour lookback
        
        # Filter gaps for just our test symbol case other real data is flowing
        my_gaps = [g for g in gaps if g["symbol"] == symbol]
        assert len(my_gaps) == 1
        gap = my_gaps[0]
        assert gap["gap_seconds"] > 800  # ~14 minutes = 840s
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM raw_ticks WHERE symbol = %s", (symbol,))
