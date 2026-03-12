"""
Benchmark: TimescaleDB hypertable vs plain PostgreSQL — on REAL market data.

Always reads from raw_ticks (populated by fetch_real_data.py with real Kraken
trades) and copies that data into a temporary plain table for comparison.
No synthetic data is ever generated here.

Usage:
    python -m module2_timescale.bench_timescale
"""

import csv
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt
import psycopg

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

BENCH_DIR = Path(__file__).parent / "bench_out"
N_TRIALS = 10
WINDOW = "3 days"     # query window used for both plain and hypertable queries


# ─── Create plain comparison table ───────────────────────────────────────────

def _create_plain_table(conn: psycopg.Connection) -> None:
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


# ─── Populate plain table from real hypertable data ──────────────────────────

def _fill_plain_from_real(conn: psycopg.Connection, window: str) -> int:
    """Copy the same real data that's in raw_ticks into the plain table."""
    with conn.cursor() as cur:
        cur.execute(f"""
            INSERT INTO raw_ticks_plain (ts, symbol, price, volume, side, order_id, trade_id)
            SELECT ts, symbol, price, volume, side, order_id, trade_id
            FROM raw_ticks
            WHERE ts >= NOW() - INTERVAL '{window}'
        """)
        count = cur.rowcount
    conn.commit()
    return count


# ─── Queries ─────────────────────────────────────────────────────────────────

def _run_plain(conn: psycopg.Connection, symbol: str, window: str) -> float:
    """Full GROUP BY scan on the plain table — no pre-aggregation."""
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT
                time_bucket('1 minute', ts) AS bucket,
                symbol,
                (array_agg(price ORDER BY ts))[1]    AS open,
                MAX(price)                            AS high,
                MIN(price)                            AS low,
                (array_agg(price ORDER BY ts DESC))[1] AS close,
                SUM(volume)                           AS volume
            FROM raw_ticks_plain
            WHERE symbol = %s
              AND ts >= NOW() - INTERVAL '{window}'
            GROUP BY bucket, symbol
            ORDER BY bucket
        """, (symbol,))
        cur.fetchall()
    return (time.perf_counter() - t0) * 1000


def _run_hyper(conn: psycopg.Connection, symbol: str, window: str) -> float:
    """Pre-computed continuous aggregate — near-instant index scan."""
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT bucket, symbol, open, high, low, close, volume
            FROM ohlcv_1m
            WHERE symbol = %s
              AND bucket >= NOW() - INTERVAL '{window}'
            ORDER BY bucket
        """, (symbol,))
        cur.fetchall()
    return (time.perf_counter() - t0) * 1000


# ─── Save benchmark run to DB ─────────────────────────────────────────────────

def _write_benchmark_run(
    conn: psycopg.Connection,
    plain_times: list,
    hyper_times: list,
    row_count: int,
) -> None:
    plain_avg = statistics.mean(plain_times)
    hyper_avg = statistics.mean(hyper_times)
    hyper_p99 = sorted(hyper_times)[int(0.99 * len(hyper_times))]
    speedup = plain_avg / max(hyper_avg, 0.001)
    conn.execute(
        """
        INSERT INTO benchmark_runs (tool, target_endpoint, duration_sec, concurrent_users,
                                    total_requests, successful_reqs, failed_reqs,
                                    peak_qps, avg_latency_ms, p99_latency_ms, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            "bench_timescale.py",
            f"3-day OHLCV (hypertable vs plain) — {row_count:,} real rows",
            0, 1, N_TRIALS * 2, N_TRIALS * 2, 0, 0,
            hyper_avg,
            hyper_p99,
            (
                f"Real Kraken data ({row_count:,} rows, window={WINDOW}) | "
                f"Plain avg={plain_avg:.1f}ms | "
                f"Hyper avg={hyper_avg:.2f}ms | "
                f"Speedup={speedup:.1f}x"
            ),
        ),
    )
    conn.commit()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(PG_DSN)
    sym = "BTC/USD"

    # Guard: make sure real data is loaded
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_ticks WHERE symbol = %s", (sym,))
        real_rows = cur.fetchone()[0]
    if real_rows == 0:
        print("x raw_ticks is empty! Run fetch_real_data.py first to load Kraken data.")
        conn.close()
        return
    print(f"✓ raw_ticks has {real_rows:,} real rows for {sym}")

    # Create plain table and fill with real data
    _create_plain_table(conn)
    copied = _fill_plain_from_real(conn, WINDOW)
    print(f"✓ Copied {copied:,} rows into raw_ticks_plain (window = {WINDOW})")

    if copied == 0:
        print(f"⚠ No rows found in the last {WINDOW}. Re-run fetch_real_data.py to refresh.")
        conn.close()
        return

    # Run benchmark
    print(f"\nRunning {WINDOW} OHLCV query {N_TRIALS}× on each (real data)...")
    plain_times: list[float] = []
    hyper_times: list[float] = []

    for i in range(N_TRIALS):
        pt = _run_plain(conn, sym, WINDOW)
        ht = _run_hyper(conn, sym, WINDOW)
        plain_times.append(pt)
        hyper_times.append(ht)
        print(f"  Trial {i+1:2d}: plain={pt:>8.2f}ms   hyper={ht:>7.2f}ms")

    plain_avg = statistics.mean(plain_times)
    plain_p99 = sorted(plain_times)[-1]
    hyper_avg = statistics.mean(hyper_times)
    hyper_p99 = sorted(hyper_times)[-1]
    speedup   = plain_avg / max(hyper_avg, 0.001)

    print(f"\n{'='*52}")
    print(f"  Dataset:     {copied:,} real Kraken rows  (window={WINDOW})")
    print(f"  Plain PG:    avg={plain_avg:.2f}ms   p99={plain_p99:.2f}ms")
    print(f"  Hypertable:  avg={hyper_avg:.2f}ms   p99={hyper_p99:.2f}ms")
    print(f"  Speedup:     {speedup:.1f}×")
    print(f"{'='*52}")

    # Save CSV
    csv_path = BENCH_DIR / "benchmark_timescale.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial", "plain_ms", "hypertable_ms"])
        for i, (pt, ht) in enumerate(zip(plain_times, hyper_times)):
            w.writerow([i + 1, f"{pt:.3f}", f"{ht:.3f}"])
    print(f"Saved {csv_path}")

    # Save chart
    fig, ax = plt.subplots(figsize=(11, 6))
    trials = list(range(1, N_TRIALS + 1))
    ax.bar([t - 0.2 for t in trials], plain_times, 0.4,
           label=f"Plain PostgreSQL (avg {plain_avg:.0f}ms)", color="#e74c3c", alpha=0.9)
    ax.bar([t + 0.2 for t in trials], hyper_times, 0.4,
           label=f"TimescaleDB hypertable (avg {hyper_avg:.1f}ms)", color="#2ecc71", alpha=0.9)
    ax.set_xlabel("Trial")
    ax.set_ylabel("Latency (ms)")
    ax.set_title(
        f"OHLCV Range Query ({WINDOW}) — Real Kraken Data\n"
        f"{copied:,} rows  |  Speedup: {speedup:.1f}×"
    )
    ax.legend()
    ax.set_xticks(trials)
    fig.tight_layout()
    png_path = BENCH_DIR / "benchmark_timescale.png"
    fig.savefig(png_path, dpi=150)
    print(f"Saved {png_path}")

    # Persist to DB
    _write_benchmark_run(conn, plain_times, hyper_times, copied)
    print("✓ Benchmark summary written to benchmark_runs table")

    # Cleanup temp table
    conn.execute("DROP TABLE IF EXISTS raw_ticks_plain CASCADE")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
