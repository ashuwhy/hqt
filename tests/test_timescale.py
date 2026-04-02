"""
tests.test_timescale
~~~~~~~~~~~~~~~~~~~~
Integration tests for Module 2 - TimescaleDB Data Layer.

Tests cover:
  - Bulk insert + VWAP validation against pandas
  - Continuous aggregate consistency (ohlcv_1m, ohlcv_5m, ohlcv_15m)
  - Compression and retention policies
  - Hypertable chunk interval validation

Run with:
    docker compose exec data-ingestor python -m pytest tests/test_timescale.py -v
"""

import math
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import psycopg
import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# ── Existing tests ────────────────────────────────────────────────────────────

async def test_timescale_analytical_functions(db_conn: psycopg.Connection, generate_symbol: str):
    """
    Insert 10k ticks, refresh ohlcv_1m, validate SQL fn_vwap against pandas VWAP.
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)
    base_price = 50000.0

    rows_to_insert = []
    for i in range(10000):
        ts = now - timedelta(minutes=10) + timedelta(seconds=i * (600.0 / 10000.0))
        price = base_price + (i % 100)
        vol = 1.0 + (i % 5)
        rows_to_insert.append((ts, symbol, price, vol, 'B', str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN'))

    with db_conn.cursor() as cur:
        with cur.copy("COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN") as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()

    with db_conn.cursor() as cur:
        try:
            start_ts = now - timedelta(minutes=15)
            end_ts = now + timedelta(minutes=1)
            cur.execute("CALL refresh_continuous_aggregate('ohlcv_1m', %s, %s);", (start_ts, end_ts))
            db_conn.commit()
        except psycopg.Error as e:
            print(f"Warning: Failed to refresh continuous aggregate: {e}")
            db_conn.rollback()

    df = pd.DataFrame(rows_to_insert, columns=["ts", "symbol", "price", "volume", "side", "order_id", "trade_id", "exchange"])
    expected_vwap = (df["price"] * df["volume"]).sum() / df["volume"].sum()

    with db_conn.cursor() as cur:
        start_ts = now - timedelta(minutes=15)
        end_ts = now + timedelta(minutes=1)
        cur.execute("SELECT fn_vwap(%s, %s, %s)", (symbol, start_ts, end_ts))
        row = cur.fetchone()
        assert row is not None
        db_vwap = row[0]

    assert math.isclose(float(db_vwap), expected_vwap, rel_tol=1e-4), f"SQL VWAP {db_vwap} != Pandas VWAP {expected_vwap}"

    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM ohlcv_1m WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        assert row is not None
        count = row[0]
        assert count >= 10, f"Expected >= 10 rows in ohlcv_1m, got {count}"


async def test_ohlcv_continuous_aggregate(db_conn: psycopg.Connection, generate_symbol: str):
    """
    Insert 100 ticks spanning 5 minutes, refresh ohlcv_1m, verify:
    - at least 5 minute-buckets
    - open ≤ high, low ≤ close, high ≥ low, volume > 0
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    rows_to_insert = []
    for i in range(100):
        ts = now - timedelta(minutes=5) + timedelta(seconds=i * 3)
        price = 1000.0 + (i % 20) * 5.0
        volume = 0.5 + (i % 10) * 0.1
        side = 'B' if i % 2 == 0 else 'S'
        rows_to_insert.append(
            (ts, symbol, price, volume, side, str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN')
        )

    with db_conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN"
        ) as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()

    start_ts = now - timedelta(minutes=6)
    end_ts = now + timedelta(minutes=1)
    try:
        with db_conn.cursor() as cur:
            cur.execute(
                "CALL refresh_continuous_aggregate('ohlcv_1m', %s, %s);",
                (start_ts, end_ts),
            )
            db_conn.commit()
    except psycopg.Error as e:
        print(f"Warning: Failed to refresh ohlcv_1m: {e}")
        db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT bucket, open, high, low, close, volume
            FROM ohlcv_1m
            WHERE symbol = %s
              AND bucket >= %s
            ORDER BY bucket
            """,
            (symbol, start_ts),
        )
        buckets = cur.fetchall()

    assert len(buckets) >= 5, (
        f"Expected at least 5 minute-buckets in ohlcv_1m, got {len(buckets)}"
    )

    for bucket, open_, high, low, close, volume in buckets:
        assert high >= low, f"bucket {bucket}: high ({high}) < low ({low})"
        assert high >= open_, f"bucket {bucket}: high ({high}) < open ({open_})"
        assert high >= close, f"bucket {bucket}: high ({high}) < close ({close})"
        assert low <= open_, f"bucket {bucket}: low ({low}) > open ({open_})"
        assert low <= close, f"bucket {bucket}: low ({low}) > close ({close})"
        assert volume > 0, f"bucket {bucket}: volume ({volume}) is not positive"


async def test_vwap_matches_pandas(db_conn: psycopg.Connection, generate_symbol: str):
    """
    Insert 50 deterministic ticks, compute python VWAP, assert fn_vwap matches.
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    rows_to_insert = []
    for i in range(50):
        ts = now - timedelta(minutes=10) + timedelta(seconds=i * 12)
        price = 200.0 + i * 1.5
        volume = 1.0 + (i % 7) * 0.5
        side = 'B' if i % 2 == 0 else 'S'
        rows_to_insert.append(
            (ts, symbol, price, volume, side, str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN')
        )

    with db_conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN"
        ) as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()

    df = pd.DataFrame(
        rows_to_insert,
        columns=["ts", "symbol", "price", "volume", "side", "order_id", "trade_id", "exchange"],
    )
    expected_vwap = float((df["price"] * df["volume"]).sum() / df["volume"].sum())

    start_ts = now - timedelta(minutes=11)
    end_ts = now + timedelta(minutes=1)
    with db_conn.cursor() as cur:
        cur.execute("SELECT fn_vwap(%s, %s, %s)", (symbol, start_ts, end_ts))
        row = cur.fetchone()

    assert row is not None, "fn_vwap returned no rows"
    db_vwap = float(row[0])

    assert math.isclose(db_vwap, expected_vwap, rel_tol=1e-4), (
        f"fn_vwap={db_vwap:.8f} does not match pandas VWAP={expected_vwap:.8f} "
        f"(rel diff={(abs(db_vwap - expected_vwap) / expected_vwap):.2e})"
    )


async def test_compression_policy(db_conn: psycopg.Connection):
    """Verify a compression policy exists for raw_ticks."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, hypertable_name, proc_name, schedule_interval
            FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_compression'
            """
        )
        jobs = cur.fetchall()

    assert len(jobs) >= 1, (
        "No compression policy found in timescaledb_information.jobs "
        "for proc_name='policy_compression'. "
        "Ensure add_compression_policy('raw_ticks', ...) was executed in init.sql."
    )

    raw_ticks_jobs = [j for j in jobs if j[1] == "raw_ticks"]
    assert len(raw_ticks_jobs) >= 1, (
        f"Compression policy exists for {[j[1] for j in jobs]} but not for 'raw_ticks'."
    )


# ── New tests ─────────────────────────────────────────────────────────────────

async def test_ohlcv_5m_aggregate(db_conn: psycopg.Connection, generate_symbol: str):
    """Insert 200 ticks spanning 25 minutes, refresh ohlcv_5m, verify ≥5 buckets."""
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    rows_to_insert = []
    for i in range(200):
        ts = now - timedelta(minutes=25) + timedelta(seconds=i * 7.5)
        price = 500.0 + (i % 50) * 2.0
        volume = 0.3 + (i % 8) * 0.15
        side = 'B' if i % 3 != 0 else 'S'
        rows_to_insert.append(
            (ts, symbol, price, volume, side, str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN')
        )

    with db_conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN"
        ) as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()

    start_ts = now - timedelta(minutes=30)
    end_ts = now + timedelta(minutes=1)
    try:
        with db_conn.cursor() as cur:
            cur.execute("CALL refresh_continuous_aggregate('ohlcv_5m', %s, %s);", (start_ts, end_ts))
            db_conn.commit()
    except psycopg.Error as e:
        print(f"Warning: Failed to refresh ohlcv_5m: {e}")
        db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT bucket, open, high, low, close, volume FROM ohlcv_5m WHERE symbol = %s AND bucket >= %s ORDER BY bucket",
            (symbol, start_ts),
        )
        buckets = cur.fetchall()

    assert len(buckets) >= 5, f"Expected ≥5 five-minute-buckets, got {len(buckets)}"
    for bucket, open_, high, low, close, volume in buckets:
        assert high >= low, f"bucket {bucket}: high < low"
        assert volume > 0, f"bucket {bucket}: volume not positive"


async def test_ohlcv_15m_aggregate(db_conn: psycopg.Connection, generate_symbol: str):
    """Insert 300 ticks spanning 75 minutes, refresh ohlcv_15m, verify ≥5 buckets."""
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    rows_to_insert = []
    for i in range(300):
        ts = now - timedelta(minutes=75) + timedelta(seconds=i * 15)
        price = 800.0 + (i % 30) * 3.0
        volume = 0.5 + (i % 6) * 0.2
        side = 'B' if i % 2 == 0 else 'S'
        rows_to_insert.append(
            (ts, symbol, price, volume, side, str(uuid.uuid4()), str(uuid.uuid4()), 'KRAKEN')
        )

    with db_conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN"
        ) as copy:
            for row in rows_to_insert:
                copy.write_row(row)
        db_conn.commit()

    start_ts = now - timedelta(minutes=80)
    end_ts = now + timedelta(minutes=1)
    try:
        with db_conn.cursor() as cur:
            cur.execute("CALL refresh_continuous_aggregate('ohlcv_15m', %s, %s);", (start_ts, end_ts))
            db_conn.commit()
    except psycopg.Error as e:
        print(f"Warning: Failed to refresh ohlcv_15m: {e}")
        db_conn.rollback()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT bucket, open, high, low, close, volume FROM ohlcv_15m WHERE symbol = %s AND bucket >= %s ORDER BY bucket",
            (symbol, start_ts),
        )
        buckets = cur.fetchall()

    assert len(buckets) >= 5, f"Expected ≥5 fifteen-minute-buckets, got {len(buckets)}"
    for bucket, open_, high, low, close, volume in buckets:
        assert high >= low, f"bucket {bucket}: high < low"
        assert volume > 0


async def test_retention_policy_exists(db_conn: psycopg.Connection):
    """Verify a retention policy exists for raw_ticks (90-day drop_chunks)."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, hypertable_name, proc_name
            FROM timescaledb_information.jobs
            WHERE proc_name = 'policy_retention'
            """
        )
        jobs = cur.fetchall()

    assert len(jobs) >= 1, "No retention policy found for proc_name='policy_retention'"
    raw_ticks_jobs = [j for j in jobs if j[1] == "raw_ticks"]
    assert len(raw_ticks_jobs) >= 1, (
        f"Retention policy exists for {[j[1] for j in jobs]} but not for 'raw_ticks'"
    )


async def test_hypertable_chunk_interval(db_conn: psycopg.Connection):
    """Verify raw_ticks chunk time interval is approximately 1 day."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT h.hypertable_name,
                   d.column_name,
                   d.time_interval
            FROM timescaledb_information.dimensions d
            JOIN timescaledb_information.hypertables h
              ON d.hypertable_name = h.hypertable_name
            WHERE h.hypertable_name = 'raw_ticks'
              AND d.column_name = 'ts'
            """
        )
        rows = cur.fetchall()

    assert len(rows) >= 1, "No dimension info found for raw_ticks.ts"
    # time_interval should be 1 day
    interval = rows[0][2]
    assert interval is not None, "time_interval is None"
    # TimescaleDB returns interval as timedelta or string
    if hasattr(interval, 'days'):
        assert interval.days == 1, f"Expected 1-day chunk interval, got {interval}"
