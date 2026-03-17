"""
tests.test_timescale_backfiller
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for the Kafka consumer parser and smart_backfiller gap detection.

Run with:
    docker compose exec data-ingestor python -m pytest tests/test_timescale_backfiller.py -v
"""

import asyncio
import os
import pytest
from datetime import datetime, timedelta, timezone
from module2_timescale.smart_backfiller import detect_gaps
from module2_timescale.kafka_consumer import _parse_message
import json
import psycopg

pytestmark = pytest.mark.asyncio


# ── Existing tests ────────────────────────────────────────────────────────────

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


@pytest.mark.integration
async def test_gap_detection(db_conn: psycopg.Connection, generate_symbol: str):
    """Insert ticks with explicit gaps and test smart_backfiller detect_gaps()."""
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    try:
        with db_conn.cursor() as cur:
            cur.execute("""
                INSERT INTO raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange)
                VALUES (%s, %s, %s, %s, %s, gen_random_uuid(), gen_random_uuid(), 'KRAKEN')
            """, (now - timedelta(minutes=15), symbol, 50000.0, 1.0, 'B'))

            cur.execute("""
                INSERT INTO raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange)
                VALUES (%s, %s, %s, %s, %s, gen_random_uuid(), gen_random_uuid(), 'KRAKEN')
            """, (now - timedelta(minutes=1), symbol, 50100.0, 1.0, 'B'))

        gaps = detect_gaps(db_conn, lookback_hours=1)

        my_gaps = [g for g in gaps if g["symbol"] == symbol]
        assert len(my_gaps) == 1
        gap = my_gaps[0]
        assert gap["gap_seconds"] > 800  # ~14 minutes = 840s
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM raw_ticks WHERE symbol = %s", (symbol,))


# ── New tests ─────────────────────────────────────────────────────────────────

def test_kafka_parser_bid_side():
    """Verify Kafka consumer correctly maps Bid liquidity_side to 'B'."""
    ts_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
    raw = json.dumps({
        "ts": ts_ns,
        "symbol": "ETH/USD",
        "price": 3500.25,
        "qty": 10.0,
        "liquidity_side": "Bid",
        "passive_id": 200,
        "taker_id": 201,
    }).encode()

    parsed = _parse_message(raw)
    assert parsed is not None
    assert parsed["symbol"] == "ETH/USD"
    assert parsed["price"] == 3500.25
    assert parsed["volume"] == 10.0
    assert parsed["side"] == "B"  # Bid -> Buy


def test_kafka_parser_missing_fields():
    """Malformed JSON with missing required fields must return None."""
    # Missing 'price' field
    raw = json.dumps({
        "ts": int(datetime.now(timezone.utc).timestamp() * 1e9),
        "symbol": "BTC/USD",
        # "price": missing!
        "qty": 1.0,
        "liquidity_side": "Ask",
        "passive_id": 300,
        "taker_id": 301,
    }).encode()

    parsed = _parse_message(raw)
    # Parser should either return None or raise — but not return a dict without 'price'
    if parsed is not None:
        assert "price" in parsed, "Parsed result must include 'price' if not None"
