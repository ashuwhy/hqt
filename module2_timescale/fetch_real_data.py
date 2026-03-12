"""
Fetch real trades from Kraken REST API (past 3 days) → raw_ticks.

Steps:
  1. DELETE all rows from raw_ticks (without CASCADE so CAs stay unlocked)
  2. Paginate Kraken /0/public/Trades, print progress every page
  3. psycopg3 binary COPY into raw_ticks (5000-row batches)
  4. refresh_continuous_aggregate for ohlcv_1m/5m/15m/1h

No API key needed.
"""

import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("fetch_real_data")

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# Kraken pair name (as returned in API response) → symbol stored in raw_ticks
PAIRS = {
    "XXBTZUSD": "BTC/USD",
    "XETHZUSD": "ETH/USD",
}

# Kraken query pair names (what you pass in the URL param)
QUERY_PAIRS = {
    "XXBTZUSD": "XBTUSD",
    "XETHZUSD": "ETHUSD",
}


KRAKEN_URL = "https://api.kraken.com/0/public/Trades"
BATCH_SIZE = 5_000
RATE_LIMIT = 1.2   # seconds between Kraken calls
DAYS_BACK = 3


# ─── 1. Wipe old data ────────────────────────────────────────────────────────

def wipe(conn: psycopg.Connection) -> None:
    log.info("Deleting all rows from raw_ticks (no CASCADE lock)...")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM raw_ticks")
        deleted = cur.rowcount
    conn.commit()
    log.info("Deleted %d rows.", deleted)


# ─── 2. Fetch from Kraken ────────────────────────────────────────────────────

def fetch_page(query_pair: str, response_key: str, since_ns: int) -> tuple[list, int]:
    """One API call. Returns (trades_list, new_since_ns).
    query_pair   = short name passed in URL param, e.g. 'XBTUSD'
    response_key = full key in JSON result dict,   e.g. 'XXBTZUSD'
    """
    resp = requests.get(
        KRAKEN_URL,
        params={"pair": query_pair, "since": since_ns, "count": 1000},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("error"):
        raise RuntimeError(f"Kraken error: {body['error']}")
    result = body["result"]
    # Kraken can return either the full key or the short name
    trades = result.get(response_key) or result.get(query_pair, [])
    last_ns = int(result["last"])
    return trades, last_ns


def to_row(trade: list, symbol: str) -> tuple:
    """Convert a Kraken trade array to a raw_ticks tuple."""
    price  = float(trade[0])
    volume = float(trade[1])
    ts_f   = float(trade[2])
    side   = "B" if trade[3] == "b" else "S"
    tid    = int(trade[6]) if len(trade) > 6 else 0
    ts     = datetime.fromtimestamp(ts_f, tz=timezone.utc)
    oid    = uuid.uuid5(uuid.NAMESPACE_OID, f"p{tid}")
    trid   = uuid.uuid5(uuid.NAMESPACE_OID, f"t{tid}")
    return (ts, symbol, price, volume, side, oid, trid, "KRAKEN")


# ─── 3. Bulk insert ──────────────────────────────────────────────────────────

def bulk_insert(conn: psycopg.Connection, rows: list[tuple]) -> None:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id, exchange) FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row(r)
    conn.commit()


# ─── 4. Refresh CAs ──────────────────────────────────────────────────────────

def refresh_cas(start: datetime, end: datetime) -> None:
    with psycopg.connect(PG_DSN, autocommit=True) as ac:
        for view in ["ohlcv_1m", "ohlcv_5m", "ohlcv_15m", "ohlcv_1h"]:
            log.info("Refreshing %s ...", view)
            ac.execute(
                f"CALL refresh_continuous_aggregate('{view}', %s, %s)",
                (start, end),
            )
            log.info("  ✓ %s done", view)


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    now     = datetime.now(timezone.utc)
    start   = now - timedelta(days=DAYS_BACK)
    since_ns = int(start.timestamp() * 1e9)

    conn = psycopg.connect(PG_DSN)

    # Step 1
    wipe(conn)

    total = 0

    # Steps 2 + 3
    for pair, symbol in PAIRS.items():
        log.info("=== %s (%s) — fetching from %s ===", symbol, pair, start.isoformat())
        cursor = since_ns
        page   = 0
        count  = 0
        batch: list[tuple] = []

        while True:
            try:
                trades, new_cursor = fetch_page(QUERY_PAIRS[pair], pair, cursor)
            except Exception as exc:
                log.warning("Request failed: %s — sleeping 5s", exc)
                time.sleep(5)
                continue

            if not trades:
                log.info("[%s] No more trades. Done.", symbol)
                break

            for t in trades:
                row = to_row(t, symbol)
                if row[0] > now:           # don't go into the future
                    continue
                batch.append(row)
                if len(batch) >= BATCH_SIZE:
                    bulk_insert(conn, batch)
                    count += len(batch)
                    batch.clear()

            page += 1
            log.info(
                "[%s] page %d | %d trades this page | total so far: %d | cursor: %d",
                symbol, page, len(trades), count + len(batch), new_cursor,
            )

            if new_cursor == cursor:
                log.info("[%s] Cursor did not advance — finished.", symbol)
                break
            if new_cursor >= int(now.timestamp() * 1e9):
                log.info("[%s] Reached present time — finished.", symbol)
                break

            cursor = new_cursor
            time.sleep(RATE_LIMIT)

        if batch:
            bulk_insert(conn, batch)
            count += len(batch)

        log.info("✓ %s: %d rows total", symbol, count)
        total += count

    conn.close()
    log.info("All pairs done. Total rows: %d", total)

    # Step 4
    log.info("Refreshing continuous aggregates (%s → %s)...", start.isoformat(), now.isoformat())
    refresh_cas(start, now + timedelta(minutes=1))
    log.info("=== COMPLETE. Real data loaded and aggregates refreshed. ===")


if __name__ == "__main__":
    main()
