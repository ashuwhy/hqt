"""
Smart Backfiller — automatically detects gaps in raw_ticks and fills them
using the Kraken REST API.

Runs as a background asyncio task inside the analytics_api.  Every
SCAN_INTERVAL_S seconds it:
  1. Queries raw_ticks for timestamp gaps > GAP_THRESHOLD per symbol.
  2. For each gap, fetches the missing trades from Kraken REST API.
  3. Bulk-inserts them into raw_ticks via psycopg3 binary COPY.
  4. Refreshes the affected continuous aggregates.

No API key required (Kraken public endpoint).
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg
import requests

logger = logging.getLogger("smart_backfiller")

# ── Config ───────────────────────────────────────────────────────────────────
PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

SCAN_INTERVAL_S = 300          # check for gaps every 5 minutes
GAP_THRESHOLD_S = 120          # gaps > 2 minutes are worth backfilling
LOOKBACK_HOURS = 6             # only scan the last 6 hours for gaps
KRAKEN_URL = "https://api.kraken.com/0/public/Trades"
RATE_LIMIT_S = 1.2             # Kraken rate limit (seconds between calls)
BATCH_SIZE = 5_000

# Kraken pair mapping (response key → symbol, query param)
PAIRS = {
    "XXBTZUSD":  {"symbol": "BTC/USD",  "query": "XBTUSD"},
    "XETHZUSD":  {"symbol": "ETH/USD",  "query": "ETHUSD"},
    "SOLUSD":    {"symbol": "SOL/USD",  "query": "SOLUSD"},
    "XXRPZUSD":  {"symbol": "XRP/USD",  "query": "XRPUSD"},
    "ADAUSD":    {"symbol": "ADA/USD",  "query": "ADAUSD"},
    "DOTUSD":    {"symbol": "DOT/USD",  "query": "DOTUSD"},
    "XDGUSD":    {"symbol": "DOGE/USD", "query": "XDGUSD"},
    "AVAXUSD":   {"symbol": "AVAX/USD", "query": "AVAXUSD"},
    "MATICUSD":  {"symbol": "MATIC/USD", "query": "MATICUSD"},
}

# Reverse lookup: symbol → pair info
SYMBOL_TO_PAIR = {v["symbol"]: {"key": k, **v} for k, v in PAIRS.items()}

# ── Backfiller state (for health endpoint) ───────────────────────────────────
backfiller_state = {
    "last_scan": None,
    "gaps_found": 0,
    "gaps_filled": 0,
    "rows_backfilled": 0,
    "running": False,
}


# ── Gap Detection ────────────────────────────────────────────────────────────
def detect_gaps(conn: psycopg.Connection, lookback_hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Find timestamp gaps > GAP_THRESHOLD_S in the last lookback_hours."""
    # Bug fix #1: psycopg3 cannot parameterise inside an interval literal string.
    # Use make_interval() and a plain numeric comparison on EXTRACT(EPOCH …) instead.
    query = """
    WITH lagged AS (
        SELECT symbol, ts,
               LAG(ts) OVER (PARTITION BY symbol ORDER BY ts) AS prev_ts
        FROM raw_ticks
        WHERE ts > now() - make_interval(hours => %s)
    )
    SELECT symbol,
           prev_ts AS gap_start,
           ts      AS gap_end,
           EXTRACT(EPOCH FROM (ts - prev_ts)) AS gap_seconds
    FROM lagged
    WHERE EXTRACT(EPOCH FROM (ts - prev_ts)) > %s
    ORDER BY gap_seconds DESC;
    """
    with conn.cursor() as cur:
        cur.execute(query, (lookback_hours, GAP_THRESHOLD_S))
        cols = [desc.name for desc in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return rows


# ── Fetch from Kraken REST API ───────────────────────────────────────────────
def fetch_trades_for_range(
    symbol: str,
    start_ts: datetime,
    end_ts: datetime,
) -> list[tuple]:
    """Fetch trades from Kraken REST API for a specific time range."""
    pair_info = SYMBOL_TO_PAIR.get(symbol)
    if not pair_info:
        logger.warning("No Kraken pair mapping for symbol %s", symbol)
        return []

    query_pair = pair_info["query"]
    response_key = pair_info["key"]
    since_ns = int(start_ts.timestamp() * 1e9)
    end_ns = int(end_ts.timestamp() * 1e9)

    all_rows: list[tuple] = []

    while since_ns < end_ns:
        try:
            resp = requests.get(
                KRAKEN_URL,
                params={"pair": query_pair, "since": since_ns, "count": 1000},
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()

            if body.get("error"):
                logger.warning("Kraken API error: %s", body["error"])
                break

            result = body["result"]
            trades = result.get(response_key) or result.get(query_pair, [])
            new_cursor = int(result["last"])

            if not trades:
                break

            for t in trades:
                price = float(t[0])
                volume = float(t[1])
                ts_f = float(t[2])
                side = "B" if t[3] == "b" else "S"
                tid = int(t[6]) if len(t) > 6 else 0

                trade_ts = datetime.fromtimestamp(ts_f, tz=timezone.utc)

                # Only include trades within the gap range
                if trade_ts > end_ts:
                    return all_rows
                if trade_ts < start_ts:
                    continue

                oid = uuid.uuid5(uuid.NAMESPACE_OID, f"bf-p{tid}")
                trid = uuid.uuid5(uuid.NAMESPACE_OID, f"bf-t{tid}")
                all_rows.append((trade_ts, symbol, price, volume, side, oid, trid))

            if new_cursor == since_ns:
                break
            since_ns = new_cursor

            time.sleep(RATE_LIMIT_S)

        except Exception as exc:
            logger.warning("Kraken fetch failed for %s: %s. Retrying in 5s...", symbol, exc)
            time.sleep(5)

    return all_rows


# ── Bulk Insert (with dedup) ─────────────────────────────────────────────────
def bulk_insert_dedup(conn: psycopg.Connection, rows: list[tuple]) -> int:
    """Insert rows into raw_ticks, skipping duplicates via ON CONFLICT."""
    if not rows:
        return 0

    attempted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        with conn.cursor() as cur:
            # Use INSERT with ON CONFLICT to avoid duplicate key errors.
            # Bug fix #3: psycopg3 executemany sets rowcount to -1 (undefined).
            # Use len(batch) as the attempted-insert count; actual skips are
            # silent via ON CONFLICT DO NOTHING which is the correct behaviour.
            cur.executemany(
                """INSERT INTO raw_ticks (ts, symbol, price, volume, side, order_id, trade_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT DO NOTHING""",
                batch,
            )
        conn.commit()
        attempted += len(batch)

    return attempted


# ── Refresh Continuous Aggregates ────────────────────────────────────────────
def refresh_aggregates(conn: psycopg.Connection, start: datetime, end: datetime) -> None:
    """Refresh continuous aggregates for the affected time range."""
    # Need autocommit for CALL
    with psycopg.connect(PG_DSN, autocommit=True) as ac:
        for view in ["ohlcv_1m", "ohlcv_5m", "ohlcv_15m", "ohlcv_1h"]:
            try:
                ac.execute(
                    f"CALL refresh_continuous_aggregate('{view}', %s, %s)",
                    (start - timedelta(hours=1), end + timedelta(hours=1)),
                )
                logger.debug("Refreshed %s for gap range", view)
            except Exception as exc:
                logger.warning("Failed to refresh %s: %s", view, exc)


# ── Main Backfill Cycle ──────────────────────────────────────────────────────
def run_backfill_cycle(conn: psycopg.Connection) -> tuple[int, int, int]:
    """Run one backfill cycle.

    Returns:
        (gaps_found, gaps_filled, total_rows_attempted)
        gaps_filled counts only gaps that had ≥1 trade returned from Kraken.
    """
    # Bug fix #5: return 3-tuple so the caller can track gaps_filled separately.
    gaps = detect_gaps(conn)

    if not gaps:
        logger.info("No gaps detected in the last %d hours", LOOKBACK_HOURS)
        return 0, 0, 0

    logger.info("Detected %d gap(s) to backfill", len(gaps))

    total_inserted = 0
    gaps_filled = 0

    for gap in gaps:
        symbol = gap["symbol"]
        gap_start = gap["gap_start"]
        gap_end = gap["gap_end"]
        gap_secs = gap["gap_seconds"]

        logger.info(
            "Backfilling %s: gap of %.0fs (%s → %s)",
            symbol, gap_secs, gap_start.isoformat(), gap_end.isoformat(),
        )

        rows = fetch_trades_for_range(symbol, gap_start, gap_end)

        if rows:
            inserted = bulk_insert_dedup(conn, rows)
            total_inserted += inserted
            gaps_filled += 1
            logger.info(
                "✓ Backfilled %s: %d trades attempted for %.0fs gap",
                symbol, inserted, gap_secs,
            )

            # Refresh aggregates for this gap range
            refresh_aggregates(conn, gap_start, gap_end)
        else:
            logger.info("No trades found on Kraken for %s gap", symbol)

    return len(gaps), gaps_filled, total_inserted


# ── Async Background Loop ────────────────────────────────────────────────────
async def run_backfiller() -> None:
    """Run the backfiller as an infinite background asyncio task."""
    global backfiller_state

    logger.info(
        "Smart backfiller started (scan every %ds, gap threshold %ds, lookback %dh)",
        SCAN_INTERVAL_S, GAP_THRESHOLD_S, LOOKBACK_HOURS,
    )
    backfiller_state["running"] = True

    # Wait a bit on startup to let the streamer establish a connection first
    await asyncio.sleep(30)

    while True:
        conn = None
        try:
            # Bug fix #4: wrap connection in try/finally to prevent leaks.
            conn = psycopg.connect(PG_DSN, autocommit=False)

            # Bug fix #2 & #5: unpack 3-tuple; use gaps_filled (not gaps_found)
            # for the filled counter so gaps with no Kraken data are not counted.
            gaps_found, gaps_filled, rows_inserted = await asyncio.to_thread(
                run_backfill_cycle, conn
            )

            backfiller_state["last_scan"] = datetime.now(timezone.utc).isoformat()
            backfiller_state["gaps_found"] += gaps_found
            backfiller_state["gaps_filled"] += gaps_filled   # ← was gaps_found
            backfiller_state["rows_backfilled"] += rows_inserted

        except asyncio.CancelledError:
            logger.info("Backfiller task cancelled")
            backfiller_state["running"] = False
            return
        except Exception as exc:
            logger.error("Backfiller cycle error: %s", exc)
        finally:
            if conn is not None:
                conn.close()

        await asyncio.sleep(SCAN_INTERVAL_S)
