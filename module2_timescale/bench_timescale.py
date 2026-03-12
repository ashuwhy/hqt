"""
Benchmark: TimescaleDB hypertable vs plain PostgreSQL table.

Loads identical data into both, runs the same OHLCV range query 10×,
compares avg + p99 latency, and generates a summary chart.

Usage:
    python -m module2_timescale.bench_timescale
"""

import csv
import os
import statistics
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import numpy as np
import psycopg

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

BENCH_DIR = Path(__file__).parent / "bench_out"
N_ROWS = 1_000_000
BATCH_SIZE = 5_000
N_TRIALS = 10


def _create_plain_table(conn: psycopg.Connection) -> None:
    """Create raw_ticks_plain (identical schema, no hypertable)."""
    conn.execute("DROP TABLE IF EXISTS raw_ticks_plain CASCADE")
    conn.execute("""
        CREATE TABLE raw_ticks_plain (
            ts          TIMESTAMPTZ    NOT NULL,
            symbol      VARCHAR(20)    NOT NULL,
            price       NUMERIC(18,8)  NOT NULL,
            volume      NUMERIC(18,8)  NOT NULL,
            side        CHAR(1)        NOT NULL,
            order_id    UUID           NOT NULL,
            trade_id    UUID           NOT NULL
        )
    """)
    conn.execute("CREATE INDEX idx_plain_symbol_ts ON raw_ticks_plain (symbol, ts DESC)")
    conn.commit()
    print("✓ Created raw_ticks_plain table")


def _generate_rows(n: int, sym: str = "BTC/USD") -> list[tuple]:
    """Generate n synthetic rows in memory."""
    rng = np.random.default_rng(42)
    price = 65000.0
    start = datetime.now(timezone.utc) - timedelta(seconds=n)
    rows = []
    for i in range(n):
        dW = rng.standard_normal()
        price *= np.exp(-0.5 * 0.02**2 + 0.02 * dW)
        ts = start + timedelta(seconds=i)
        side = "B" if rng.random() < 0.5 else "S"
        vol = round(rng.uniform(0.01, 10.0), 8)
        rows.append((ts, sym, round(price, 8), vol, side, uuid.uuid4(), uuid.uuid4()))
    return rows


def _bulk_load(conn: psycopg.Connection, table: str, rows: list[tuple]) -> None:
    """COPY rows into the specified table."""
    total = len(rows)
    for start in range(0, total, BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        with conn.cursor() as cur:
            with cur.copy(
                f"COPY {table} (ts, symbol, price, volume, side, order_id, trade_id) FROM STDIN"
            ) as copy:
                for row in batch:
                    copy.write_row(row)
        conn.commit()
    print(f"  ✓ Loaded {total:,} rows into {table}")


def _run_ohlcv_query(conn: psycopg.Connection, table: str, symbol: str) -> float:
    """Run an OHLCV range query and return elapsed ms."""
    # For the plain table, we compute OHLCV inline
    query = f"""
        SELECT
            time_bucket('1 minute', ts) AS bucket,
            symbol,
            (array_agg(price ORDER BY ts))[1] AS open,
            MAX(price) AS high,
            MIN(price) AS low,
            (array_agg(price ORDER BY ts DESC))[1] AS close,
            SUM(volume) AS volume
        FROM {table}
        WHERE symbol = %s
          AND ts >= NOW() - INTERVAL '1 hour'
        GROUP BY bucket, symbol
        ORDER BY bucket
    """
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(query, (symbol,))
        cur.fetchall()
    return (time.perf_counter() - t0) * 1000  # ms


def _run_ohlcv_hypertable(conn: psycopg.Connection, symbol: str) -> float:
    """Run the same query using the continuous aggregate."""
    query = """
        SELECT bucket, symbol, open, high, low, close, volume
        FROM ohlcv_1m
        WHERE symbol = %s
          AND bucket >= NOW() - INTERVAL '1 hour'
        ORDER BY bucket
    """
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(query, (symbol,))
        cur.fetchall()
    return (time.perf_counter() - t0) * 1000


def _write_benchmark_run(conn: psycopg.Connection, plain_times: list, hyper_times: list) -> None:
    """Write summary to benchmark_runs table."""
    conn.execute(
        """
        INSERT INTO benchmark_runs (tool, target_endpoint, duration_sec, concurrent_users,
                                    total_requests, successful_reqs, failed_reqs,
                                    peak_qps, avg_latency_ms, p99_latency_ms, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            "bench_timescale.py",
            "OHLCV range query (hypertable vs plain)",
            0,
            1,
            N_TRIALS * 2,
            N_TRIALS * 2,
            0,
            0,
            statistics.mean(hyper_times),
            sorted(hyper_times)[int(0.99 * len(hyper_times))],
            f"Plain avg={statistics.mean(plain_times):.2f}ms p99={sorted(plain_times)[-1]:.2f}ms | "
            f"Hyper avg={statistics.mean(hyper_times):.2f}ms p99={sorted(hyper_times)[-1]:.2f}ms | "
            f"Speedup={statistics.mean(plain_times)/max(statistics.mean(hyper_times),0.001):.1f}x",
        ),
    )
    conn.commit()


def main() -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(PG_DSN)
    sym = "BTC/USD"

    print(f"Generating {N_ROWS:,} rows...")
    rows = _generate_rows(N_ROWS, sym)

    # Create and load plain table
    _create_plain_table(conn)
    print("Loading into raw_ticks_plain...")
    _bulk_load(conn, "raw_ticks_plain", rows)

    # Check if hypertable already has data; if not, load
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_ticks WHERE symbol = %s", (sym,))
        existing = cur.fetchone()[0]
    if existing < N_ROWS:
        print("Loading into raw_ticks (hypertable)...")
        _bulk_load(conn, "raw_ticks", rows)

    # Refresh continuous aggregate so ohlcv_1m has data
    print("Refreshing ohlcv_1m continuous aggregate...")
    oldest_ts = rows[0][0]
    newest_ts = rows[-1][0] + timedelta(minutes=1)
    with psycopg.connect(PG_DSN, autocommit=True) as auto_conn:
        auto_conn.execute(
            "CALL refresh_continuous_aggregate('ohlcv_1m', %s, %s)",
            (oldest_ts, newest_ts),
        )

    # Run benchmark
    print(f"\nRunning OHLCV query {N_TRIALS}× on each...")
    plain_times = []
    hyper_times = []
    for i in range(N_TRIALS):
        pt = _run_ohlcv_query(conn, "raw_ticks_plain", sym)
        ht = _run_ohlcv_hypertable(conn, sym)
        plain_times.append(pt)
        hyper_times.append(ht)
        print(f"  Trial {i+1}: plain={pt:.2f}ms  hyper={ht:.2f}ms")

    # Stats
    plain_avg = statistics.mean(plain_times)
    plain_p99 = sorted(plain_times)[int(0.99 * N_TRIALS)]
    hyper_avg = statistics.mean(hyper_times)
    hyper_p99 = sorted(hyper_times)[int(0.99 * N_TRIALS)]
    speedup = plain_avg / max(hyper_avg, 0.001)

    print(f"\n{'='*50}")
    print(f"Plain PG:    avg={plain_avg:.2f}ms  p99={plain_p99:.2f}ms")
    print(f"Hypertable:  avg={hyper_avg:.2f}ms  p99={hyper_p99:.2f}ms")
    print(f"Speedup:     {speedup:.1f}×")
    print(f"{'='*50}")

    # Save CSV
    csv_path = BENCH_DIR / "benchmark_timescale.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial", "plain_ms", "hypertable_ms"])
        for i, (pt, ht) in enumerate(zip(plain_times, hyper_times)):
            w.writerow([i + 1, round(pt, 3), round(ht, 3)])
    print(f"Saved {csv_path}")

    # Save chart
    fig, ax = plt.subplots(figsize=(10, 6))
    trials = list(range(1, N_TRIALS + 1))
    ax.bar([t - 0.2 for t in trials], plain_times, 0.4, label="Plain PostgreSQL", color="#e74c3c")
    ax.bar([t + 0.2 for t in trials], hyper_times, 0.4, label="TimescaleDB Hypertable", color="#2ecc71")
    ax.set_xlabel("Trial")
    ax.set_ylabel("Latency (ms)")
    ax.set_title("OHLCV Range Query: TimescaleDB vs Plain PostgreSQL")
    ax.legend()
    ax.set_xticks(trials)
    fig.tight_layout()
    png_path = BENCH_DIR / "benchmark_timescale.png"
    fig.savefig(png_path, dpi=150)
    print(f"Saved {png_path}")

    # Write to benchmark_runs
    _write_benchmark_run(conn, plain_times, hyper_times)
    print("✓ Benchmark summary written to benchmark_runs table")

    # Cleanup
    conn.execute("DROP TABLE IF EXISTS raw_ticks_plain CASCADE")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
