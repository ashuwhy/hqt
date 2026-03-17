"""
Synthetic GBM tick data generator for TimescaleDB benchmarking.

Generates Geometric Brownian Motion price series and bulk-inserts via
psycopg3 binary COPY for maximum throughput (target >= 500k rows/min).

Usage:
    python -m module2_timescale.gen_ticks --rows 1000000
    python -m module2_timescale.gen_ticks --rows 100000 --symbols BTC/USD,ETH/USD,SOL/USD
    python -m module2_timescale.gen_ticks --rows 500000 --batch-size 10000 --truncate
"""

import argparse
import math
import os
import time
import uuid
from datetime import datetime, timezone, timedelta

import numpy as np
import psycopg

# ─── DSN ──────────────────────────────────────────────────────────────────────

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# ─── GBM price generation ─────────────────────────────────────────────────────

def _gbm_prices(n: int, s0: float, mu: float = 0.0, sigma: float = 0.02, dt: float = 1.0) -> np.ndarray:
    """
    Generate n GBM price steps.

    S[t+1] = S[t] * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z),  Z ~ N(0,1)

    Returns array of shape (n,) with prices starting at s0.
    """
    z = np.random.standard_normal(n - 1)
    log_returns = (mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z
    log_prices = np.empty(n)
    log_prices[0] = math.log(s0)
    log_prices[1:] = math.log(s0) + np.cumsum(log_returns)
    return np.exp(log_prices)


# ─── Row generation ───────────────────────────────────────────────────────────

def _generate_rows(
    symbols: list[str],
    total_rows: int,
    start_price: float,
) -> list[tuple]:
    """
    Pre-generate all rows in memory using numpy for speed.

    Timestamps start at NOW() - total_rows seconds and step 1 second each,
    spread round-robin across symbols.

    Returns list of (ts, symbol, price, volume, side, order_id, trade_id) tuples.
    """
    now = datetime.now(timezone.utc)
    origin = now - timedelta(seconds=total_rows)

    rows_per_sym = total_rows // len(symbols)
    remainder = total_rows % len(symbols)

    all_rows: list[tuple] = []

    for sym_idx, symbol in enumerate(symbols):
        n = rows_per_sym + (1 if sym_idx < remainder else 0)
        if n == 0:
            continue

        prices = _gbm_prices(n, start_price)
        volumes = np.random.uniform(0.01, 10.0, n)
        sides_mask = np.random.randint(0, 2, n)  # 0 -> 'B', 1 -> 'S'

        # Each symbol gets its own sequential timestamp slice so records interleave
        # when sorted by ts (like real data), but generation is per-symbol for speed.
        sym_start = origin + timedelta(seconds=sym_idx)

        for i in range(n):
            ts = sym_start + timedelta(seconds=i * len(symbols))
            price = float(prices[i])
            volume = float(volumes[i])
            side = 'B' if sides_mask[i] == 0 else 'S'
            order_id = uuid.uuid4()
            trade_id = uuid.uuid4()
            all_rows.append((ts, symbol, price, volume, side, order_id, trade_id))

    return all_rows


# ─── Bulk insert ──────────────────────────────────────────────────────────────

def _bulk_insert(
    conn: psycopg.Connection,
    rows: list[tuple],
    batch_size: int,
) -> None:
    """
    Insert rows via psycopg3 COPY for maximum throughput.
    Prints progress and rows/sec after each batch.
    """
    total = len(rows)
    inserted = 0
    t_start = time.perf_counter()
    t_last = t_start

    while inserted < total:
        batch = rows[inserted : inserted + batch_size]

        with conn.cursor() as cur:
            with cur.copy(
                "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id) FROM STDIN"
            ) as copy:
                for row in batch:
                    copy.write_row(row)
        conn.commit()

        inserted += len(batch)
        now = time.perf_counter()
        elapsed = now - t_last
        batch_rps = len(batch) / elapsed if elapsed > 0 else 0
        total_elapsed = now - t_start
        overall_rps = inserted / total_elapsed if total_elapsed > 0 else 0
        t_last = now

        print(
            f"  Inserted {inserted:>10,}/{total:,} rows  "
            f"({batch_rps:>8,.0f} rows/sec this batch | "
            f"{overall_rps:>8,.0f} rows/sec overall)"
        )


# ─── Verification ─────────────────────────────────────────────────────────────

def _verify_count(conn: psycopg.Connection, expected: int) -> None:
    """Verify row count is within ±0.1% of expected."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_ticks")
        actual = cur.fetchone()[0]

    tolerance = expected * 0.001
    lo = expected - tolerance
    hi = expected + tolerance

    if lo <= actual <= hi:
        print(f"\n  Verification PASSED: count={actual:,} (expected {expected:,} ± 0.1%)")
    else:
        print(
            f"\n  Verification FAILED: count={actual:,} "
            f"(expected {expected:,} ± {int(tolerance):,})"
        )


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate synthetic GBM tick data into raw_ticks via psycopg3 COPY."
    )
    p.add_argument(
        "--rows",
        type=int,
        default=1_000_000,
        help="Total number of rows to insert (default: 1,000,000)",
    )
    p.add_argument(
        "--symbols",
        type=str,
        default="BTC/USD,ETH/USD",
        help="Comma-separated list of symbols (default: BTC/USD,ETH/USD)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=5_000,
        help="Rows per COPY batch (default: 5,000)",
    )
    p.add_argument(
        "--start-price",
        type=float,
        default=50_000.0,
        help="Initial price for all symbols (default: 50,000.0)",
    )
    p.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate raw_ticks before inserting",
    )
    return p.parse_args()


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    args = _parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    print("=" * 60)
    print(f"  GBM tick generator")
    print(f"  Rows:       {args.rows:,}")
    print(f"  Symbols:    {', '.join(symbols)}")
    print(f"  Batch size: {args.batch_size:,}")
    print(f"  Start price:{args.start_price:,.2f}")
    print(f"  Truncate:   {args.truncate}")
    print("=" * 60)

    # Connect
    conn = psycopg.connect(PG_DSN)

    if args.truncate:
        conn.execute("TRUNCATE TABLE raw_ticks")
        conn.commit()
        print("  Truncated raw_ticks.")

    # Generate
    print(f"\nGenerating {args.rows:,} rows (numpy GBM)...")
    t_gen_start = time.perf_counter()
    rows = _generate_rows(symbols, args.rows, args.start_price)
    t_gen = time.perf_counter() - t_gen_start
    print(f"  Generated {len(rows):,} rows in {t_gen:.2f}s")

    # Insert
    print(f"\nInserting via COPY (batch_size={args.batch_size:,})...")
    t_ins_start = time.perf_counter()
    _bulk_insert(conn, rows, args.batch_size)
    t_ins = time.perf_counter() - t_ins_start

    rows_per_min = len(rows) / t_ins * 60
    print(f"\n  Insert completed in {t_ins:.2f}s  ({rows_per_min:,.0f} rows/min)")

    if rows_per_min < 500_000:
        print(f"  WARNING: throughput {rows_per_min:,.0f} rows/min < 500k target")
    else:
        print(f"  Throughput target MET: {rows_per_min:,.0f} rows/min >= 500k")

    # Verify
    _verify_count(conn, len(rows))

    # Summary
    total_elapsed = t_gen + t_ins
    print(f"\n{'='*60}")
    print(f"  Total time:  {total_elapsed:.2f}s (gen={t_gen:.2f}s, insert={t_ins:.2f}s)")
    print(f"  Throughput:  {rows_per_min:,.0f} rows/min")
    print(f"{'='*60}")

    conn.close()


if __name__ == "__main__":
    main()
