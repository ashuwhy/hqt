"""
Benchmark: TimescaleDB hypertable vs plain PostgreSQL - on REAL market data.

Always reads from raw_ticks (populated by fetch_real_data.py with real Kraken
trades) and copies that data into a temporary plain table for comparison.
If raw_ticks has fewer than 10,000 rows, auto-runs gen_ticks.py with 100k
synthetic rows so the benchmark has meaningful data to work with.

Usage:
    python -m module2_timescale.bench_timescale
    python -m module2_timescale.bench_timescale --quick
"""

import argparse
import csv
import os
import statistics
import subprocess
import sys
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
WINDOW = "3 days"     # query window used for both plain and hypertable queries
MIN_ROWS = 10_000     # auto-seed threshold
AUTO_SEED_ROWS = 100_000


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Benchmark TimescaleDB hypertable vs plain PostgreSQL.")
    p.add_argument(
        "--quick",
        action="store_true",
        help="Run 3 iterations instead of 10 (for CI / fast feedback)",
    )
    return p.parse_args()


# ─── Auto-seed ────────────────────────────────────────────────────────────────

def _auto_seed(n_rows: int = AUTO_SEED_ROWS) -> None:
    """
    Invoke gen_ticks.py as a subprocess to populate raw_ticks with synthetic data.
    Exits with a non-zero code if the seed step fails.
    """
    print(
        f"raw_ticks has fewer than {MIN_ROWS:,} rows. "
        f"Auto-seeding {n_rows:,} synthetic rows via gen_ticks.py..."
    )
    cmd = [
        sys.executable, "-m", "module2_timescale.gen_ticks",
        "--rows", str(n_rows),
        "--symbols", "BTC/USD,ETH/USD",
        "--batch-size", "5000",
    ]
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        print(f"ERROR: gen_ticks.py exited with code {result.returncode}. Aborting benchmark.")
        sys.exit(result.returncode)
    print(f"Auto-seed complete.")


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
    """Full GROUP BY scan on the plain table - no pre-aggregation."""
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
    """Pre-computed continuous aggregate - near-instant index scan."""
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
    n_trials: int,
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
            "raw_ticks vs raw_ticks_plain",
            0, 1, n_trials * 2, n_trials * 2, 0, 0,
            hyper_avg,
            hyper_p99,
            (
                f"hypertable speedup: {speedup:.1f}x | "
                f"Real/synthetic data ({row_count:,} rows, window={WINDOW}) | "
                f"Plain avg={plain_avg:.1f}ms | "
                f"Hyper avg={hyper_avg:.2f}ms"
            ),
        ),
    )
    conn.commit()


# ─── Chart ────────────────────────────────────────────────────────────────────

def _save_chart(
    plain_times: list[float],
    hyper_times: list[float],
    copied: int,
    speedup: float,
    n_trials: int,
) -> Path:
    plain_avg = statistics.mean(plain_times)
    hyper_avg = statistics.mean(hyper_times)

    fig, ax = plt.subplots(figsize=(11, 6))
    trials = list(range(1, n_trials + 1))

    plain_bars = ax.bar(
        [t - 0.2 for t in trials], plain_times, 0.4,
        label=f"Plain PostgreSQL (avg {plain_avg:.0f} ms)", color="#e74c3c", alpha=0.9,
    )
    hyper_bars = ax.bar(
        [t + 0.2 for t in trials], hyper_times, 0.4,
        label=f"TimescaleDB hypertable (avg {hyper_avg:.1f} ms)", color="#2ecc71", alpha=0.9,
    )

    # Label speedup ratio on each pair of bars
    for i, (pt, ht) in enumerate(zip(plain_times, hyper_times)):
        ratio = pt / max(ht, 0.001)
        x_pos = trials[i]  # centre between the two bars
        y_pos = max(pt, ht) * 1.03
        ax.text(
            x_pos, y_pos, f"{ratio:.1f}×",
            ha="center", va="bottom", fontsize=7.5, color="#2c3e50",
        )

    ax.set_xlabel("Trial")
    ax.set_ylabel("Latency (ms)")
    ax.set_title(
        f"TimescaleDB Hypertable vs Plain PostgreSQL - OHLCV Range Query ({WINDOW})\n"
        f"{copied:,} rows  |  Overall speedup: {speedup:.1f}×"
    )
    ax.legend()
    ax.set_xticks(trials)
    fig.tight_layout()

    png_path = BENCH_DIR / "benchmark_timescale.png"
    fig.savefig(png_path, dpi=150)
    plt.close(fig)
    return png_path


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    n_trials = 3 if args.quick else 10

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(PG_DSN)
    sym = "BTC/USD"

    # Guard: ensure raw_ticks has enough rows; auto-seed with synthetic data if not
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_ticks")
        total_rows = cur.fetchone()[0]

    if total_rows < MIN_ROWS:
        conn.close()
        _auto_seed(AUTO_SEED_ROWS)
        # Reconnect after seeding
        conn = psycopg.connect(PG_DSN)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw_ticks WHERE symbol = %s", (sym,))
            real_rows = cur.fetchone()[0]
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw_ticks WHERE symbol = %s", (sym,))
            real_rows = cur.fetchone()[0]

    if real_rows == 0:
        print(
            f"raw_ticks has {total_rows:,} total rows but none for {sym}. "
            "Run fetch_real_data.py or gen_ticks.py with --symbols BTC/USD."
        )
        conn.close()
        return
    print(f"raw_ticks has {real_rows:,} rows for {sym}")

    # Create plain table and fill with real data
    _create_plain_table(conn)
    copied = _fill_plain_from_real(conn, WINDOW)
    print(f"Copied {copied:,} rows into raw_ticks_plain (window = {WINDOW})")

    if copied == 0:
        print(f"No rows found in the last {WINDOW}. Re-run fetch_real_data.py or gen_ticks.py to refresh.")
        conn.close()
        return

    # Run benchmark
    print(f"\nRunning {WINDOW} OHLCV query {n_trials}x on each (plain vs hypertable)...")
    plain_times: list[float] = []
    hyper_times: list[float] = []

    for i in range(n_trials):
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
    print(f"  Dataset:     {copied:,} rows  (window={WINDOW})")
    print(f"  Plain PG:    avg={plain_avg:.2f}ms   p99={plain_p99:.2f}ms")
    print(f"  Hypertable:  avg={hyper_avg:.2f}ms   p99={hyper_p99:.2f}ms")
    print(f"  Speedup:     {speedup:.1f}x")
    print(f"{'='*52}")

    # Save CSV
    csv_path = BENCH_DIR / "benchmark_timescale.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["trial", "plain_ms", "hypertable_ms"])
        for i, (pt, ht) in enumerate(zip(plain_times, hyper_times)):
            w.writerow([i + 1, f"{pt:.3f}", f"{ht:.3f}"])
    print(f"Saved {csv_path}")

    # Save chart at 150 DPI with speedup labels on bars
    png_path = _save_chart(plain_times, hyper_times, copied, speedup, n_trials)
    print(f"Saved {png_path}")

    # Persist to DB
    _write_benchmark_run(conn, plain_times, hyper_times, copied, n_trials)
    print("Benchmark summary written to benchmark_runs table")

    # Cleanup temp table
    conn.execute("DROP TABLE IF EXISTS raw_ticks_plain CASCADE")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
