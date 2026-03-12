"""
Synthetic tick generator using Geometric Brownian Motion (GBM).
Fetches REAL current prices from CoinGecko (free, no API key, no geo-restriction)
and uses them as seed prices.

Usage:
    python -m module2_timescale.gen_ticks --rows 1000000 --symbols BTC/USD,ETH/USD --batch-size 5000
"""

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np
import psycopg

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# Mapping from our symbols → CoinGecko IDs
COINGECKO_IDS = {
    "BTC/USD": "bitcoin",
    "ETH/USD": "ethereum",
    "BNB/USD": "binancecoin",
    "SOL/USD": "solana",
    "ADA/USD": "cardano",
    "XRP/USD": "ripple",
    "DOGE/USD": "dogecoin",
    "DOT/USD": "polkadot",
    "AVAX/USD": "avalanche-2",
    "MATIC/USD": "matic-network",
}

# Fallback seed prices if CoinGecko is unreachable
FALLBACK_PRICES = {
    "BTC/USD": 65000.0,
    "ETH/USD": 3500.0,
    "BNB/USD": 600.0,
    "SOL/USD": 150.0,
    "ADA/USD": 0.45,
    "XRP/USD": 0.55,
    "DOGE/USD": 0.12,
    "DOT/USD": 7.50,
    "AVAX/USD": 35.0,
    "MATIC/USD": 0.85,
}

# GBM parameters
MU = 0.0       # drift
SIGMA = 0.02   # volatility
DT = 1.0       # 1 second per tick


def fetch_real_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch current USD prices from CoinGecko free API (no key required)."""
    gecko_ids = []
    sym_to_id = {}
    for sym in symbols:
        gid = COINGECKO_IDS.get(sym)
        if gid:
            gecko_ids.append(gid)
            sym_to_id[gid] = sym

    if not gecko_ids:
        print("  ⚠ No CoinGecko mappings found, using fallback prices")
        return {s: FALLBACK_PRICES.get(s, 100.0) for s in symbols}

    ids_param = ",".join(gecko_ids)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids_param}&vs_currencies=usd"

    try:
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "HQT/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        prices = {}
        for gid, sym in sym_to_id.items():
            if gid in data and "usd" in data[gid]:
                prices[sym] = float(data[gid]["usd"])
                print(f"  ✓ {sym}: ${prices[sym]:,.2f} (live from CoinGecko)")
            else:
                prices[sym] = FALLBACK_PRICES.get(sym, 100.0)
                print(f"  ⚠ {sym}: ${prices[sym]:,.2f} (fallback)")
        return prices

    except (URLError, json.JSONDecodeError, OSError) as exc:
        print(f"  ⚠ CoinGecko unavailable ({exc}), using fallback prices")
        return {s: FALLBACK_PRICES.get(s, 100.0) for s in symbols}


def generate_ticks(
    n_rows: int,
    symbols: list[str],
    batch_size: int,
    rng: np.random.Generator | None = None,
    use_real_prices: bool = True,
) -> None:
    """Generate and insert n_rows synthetic ticks into raw_ticks."""
    if rng is None:
        rng = np.random.default_rng(42)

    print("Fetching seed prices...")
    if use_real_prices:
        seed_prices = fetch_real_prices(symbols)
    else:
        seed_prices = {s: FALLBACK_PRICES.get(s, 100.0) for s in symbols}
        for s in symbols:
            print(f"  ✓ {s}: ${seed_prices[s]:,.2f} (hardcoded)")

    conn = psycopg.connect(PG_DSN, autocommit=False)
    rows_per_symbol = n_rows // len(symbols)
    total_inserted = 0
    t_start = time.monotonic()

    print(f"\nGenerating {n_rows:,} ticks across {symbols} (batch_size={batch_size})")

    for sym in symbols:
        price = seed_prices.get(sym, 100.0)
        start_ts = datetime.now(timezone.utc) - timedelta(seconds=rows_per_symbol)
        batch: list[tuple] = []

        for i in range(rows_per_symbol):
            # GBM step: dS = S * (μ * dt + σ * dW)
            dW = rng.standard_normal()
            price *= np.exp((MU - 0.5 * SIGMA**2) * DT + SIGMA * np.sqrt(DT) * dW)
            price = max(price, 1e-8)  # floor at near-zero

            ts = start_ts + timedelta(seconds=i)
            side = "B" if rng.random() < 0.5 else "S"
            volume = round(rng.uniform(0.01, 10.0), 8)
            order_id = uuid.uuid4()
            trade_id = uuid.uuid4()

            batch.append((ts, sym, round(price, 8), volume, side, order_id, trade_id))

            if len(batch) >= batch_size:
                _copy_batch(conn, batch)
                total_inserted += len(batch)
                elapsed = time.monotonic() - t_start
                rate = total_inserted / elapsed if elapsed > 0 else 0
                print(
                    f"\r  {sym}: {total_inserted:>10,} rows  "
                    f"({rate:,.0f} rows/s)",
                    end="",
                    flush=True,
                )
                batch.clear()

        # Flush remaining
        if batch:
            _copy_batch(conn, batch)
            total_inserted += len(batch)
            batch.clear()

        print()  # newline after symbol

    conn.close()
    elapsed = time.monotonic() - t_start
    rate = total_inserted / elapsed if elapsed > 0 else 0
    print(f"\nDone: {total_inserted:,} rows in {elapsed:.1f}s ({rate:,.0f} rows/s)")


def _copy_batch(conn: psycopg.Connection, rows: list[tuple]) -> None:
    """Binary COPY a batch of rows into raw_ticks."""
    with conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id) "
            "FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(row)
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic tick data")
    parser.add_argument("--rows", type=int, default=1_000_000, help="Total rows to generate")
    parser.add_argument("--symbols", type=str, default="BTC/USD,ETH/USD", help="Comma-separated symbols")
    parser.add_argument("--batch-size", type=int, default=5000, help="Rows per COPY batch")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--no-live-prices", action="store_true", help="Skip CoinGecko, use hardcoded fallback prices")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    rng = np.random.default_rng(args.seed)
    generate_ticks(args.rows, symbols, args.batch_size, rng, use_real_prices=not args.no_live_prices)


if __name__ == "__main__":
    main()
