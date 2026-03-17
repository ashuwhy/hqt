import math
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd
import psycopg
import pytest

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


async def test_ohlcv_continuous_aggregate(db_conn: psycopg.Connection, generate_symbol: str):
    """
    Insert 100 ticks for a unique symbol spanning 5 minutes, refresh the
    ohlcv_1m continuous aggregate, then verify:
    - at least 5 minute-buckets are present
    - open, high, low, close values are internally consistent for each bucket
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    # 100 ticks, one every 3 seconds → spans 5 minutes
    rows_to_insert = []
    for i in range(100):
        ts = now - timedelta(minutes=5) + timedelta(seconds=i * 3)
        price = 1000.0 + (i % 20) * 5.0   # oscillates between 1000 and 1095
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

    # Refresh the aggregate over the inserted range
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

    # Query ohlcv_1m for the symbol
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
    Insert 50 ticks with deterministic prices and volumes, compute the expected
    VWAP in Python, then assert that fn_vwap() returns a value within 4 decimal
    places (rel_tol=1e-4).
    """
    symbol = generate_symbol
    now = datetime.now(timezone.utc)

    rows_to_insert = []
    for i in range(50):
        ts = now - timedelta(minutes=10) + timedelta(seconds=i * 12)
        price = 200.0 + i * 1.5        # 200.0, 201.5, 203.0, ... 273.5
        volume = 1.0 + (i % 7) * 0.5   # 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, repeating
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

    # Python/pandas expected VWAP
    df = pd.DataFrame(
        rows_to_insert,
        columns=["ts", "symbol", "price", "volume", "side", "order_id", "trade_id", "exchange"],
    )
    expected_vwap = float((df["price"] * df["volume"]).sum() / df["volume"].sum())

    # SQL VWAP via fn_vwap
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
    """
    Verify that a compression policy exists for the raw_ticks hypertable by
    querying timescaledb_information.jobs for proc_name = 'policy_compression'.
    """
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

    # Confirm at least one policy targets raw_ticks specifically
    raw_ticks_jobs = [j for j in jobs if j[1] == "raw_ticks"]
    assert len(raw_ticks_jobs) >= 1, (
        f"Compression policy exists for {[j[1] for j in jobs]} but not for 'raw_ticks'."
    )
