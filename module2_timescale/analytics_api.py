"""
HQT TimescaleDB Analytics REST API.

Endpoints:
    GET /health, /analytics/health
    GET /analytics/ticks
    GET /analytics/ohlcv
    GET /analytics/indicators
"""

import asyncio
import logging
import os

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

import psycopg
from psycopg.rows import dict_row

from module2_timescale.kafka_consumer import run_consumer
from module2_timescale.live_streamer import stream_trades
from module2_timescale.smart_backfiller import run_backfiller, backfiller_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("analytics_api")

# ── Database ─────────────────────────────────────────────────────────────────
PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# Interval name → continuous aggregate view
INTERVAL_MAP = {
    "1m":  "ohlcv_1m",
    "5m":  "ohlcv_5m",
    "15m": "ohlcv_15m",
    "1h":  "ohlcv_1h",
}

# ── Lifespan ─────────────────────────────────────────────────────────────────
consumer_task: asyncio.Task | None = None
streamer_task: asyncio.Task | None = None
backfiller_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global consumer_task, streamer_task, backfiller_task
    # Load SQL indicator functions on startup
    try:
        conn = psycopg.connect(PG_DSN)
        indicators_path = os.path.join(os.path.dirname(__file__), "indicators.sql")
        if os.path.exists(indicators_path):
            with open(indicators_path) as f:
                conn.execute(f.read())
            conn.commit()
            logger.info("SQL indicator functions loaded")

        # ── Verify real data is present ──────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM raw_ticks")
            row_count = cur.fetchone()[0]

        if row_count < 1000:
            logger.warning(
                "raw_ticks has only %d rows - real data may not be loaded. "
                "Triggering fetch_real_data in background...", row_count
            )
            asyncio.create_task(_auto_fetch_real_data())
        else:
            logger.info("raw_ticks verified: %d real rows loaded", row_count)

        conn.close()
    except Exception as exc:
        logger.warning("Startup check failed: %s", exc)

    # Start Kafka consumer background task
    consumer_task = asyncio.create_task(run_consumer())
    logger.info("Kafka consumer background task started")
    
    # Start live streamer background task (duration 0 = infinite)
    pairs = [
        "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", 
        "ADA/USD", "DOT/USD", "DOGE/USD", "AVAX/USD", "MATIC/USD"
    ]
    streamer_task = asyncio.create_task(stream_trades(pairs, 0))
    logger.info("Kraken live streamer background task started for %d pairs", len(pairs))
    
    # Start smart backfiller background task
    backfiller_task = asyncio.create_task(run_backfiller())
    logger.info("Smart backfiller background task started")
    
    yield
    
    # Shutdown
    if consumer_task:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
            
    if streamer_task:
        streamer_task.cancel()
        try:
            await streamer_task
        except asyncio.CancelledError:
            pass
            
    if backfiller_task:
        backfiller_task.cancel()
        try:
            await backfiller_task
        except asyncio.CancelledError:
            pass


async def _auto_fetch_real_data() -> None:
    """Run fetch_real_data.py as a subprocess to populate raw_ticks with real Kraken data."""
    logger.info("Auto-fetching real Kraken data...")
    try:
        env = os.environ.copy()
        proc = await asyncio.create_subprocess_exec(
            "python", "-u", "-m", "module2_timescale.fetch_real_data",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Real data fetch complete.\n%s", stdout.decode())
        else:
            logger.error("fetch_real_data failed (rc=%d):\n%s", proc.returncode, stdout.decode())
    except Exception as exc:
        logger.error("Auto-fetch error: %s", exc)


app = FastAPI(title="HQT Timescale Analytics API", lifespan=lifespan)


def _get_conn():
    return psycopg.connect(PG_DSN, row_factory=dict_row)


# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
@app.get("/analytics/health")
async def health():
    global consumer_task, streamer_task, backfiller_task
    
    health_data = {
        "status": "ok",
        "module": "timescale_analytics",
        "consumer_status": "stopped",
        "streamer_status": "stopped",
        "backfiller_status": "stopped",
        "backfiller_stats": backfiller_state,
        "row_count": 0
    }

    if consumer_task:
        if consumer_task.done():
            if consumer_task.exception():
                health_data["consumer_status"] = f"crashed: {consumer_task.exception()}"
                health_data["status"] = "degraded"
            else:
                health_data["consumer_status"] = "finished"
        else:
            health_data["consumer_status"] = "running"
            
    if streamer_task:
        if streamer_task.done():
            if streamer_task.exception():
                health_data["streamer_status"] = f"crashed: {streamer_task.exception()}"
                health_data["status"] = "degraded"
            else:
                health_data["streamer_status"] = "finished"
        else:
            health_data["streamer_status"] = "running"

    if backfiller_task:
        if backfiller_task.done():
            if backfiller_task.exception():
                health_data["backfiller_status"] = f"crashed: {backfiller_task.exception()}"
                health_data["status"] = "degraded"
            else:
                health_data["backfiller_status"] = "finished"
        else:
            health_data["backfiller_status"] = "running"

    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS cnt FROM raw_ticks")
            row_count = cur.fetchone()["cnt"]
        conn.close()
        health_data["row_count"] = row_count
    except Exception as exc:
        health_data["status"] = "degraded"
        health_data["error"] = str(exc)
        
    return health_data


# ── GET /analytics/ticks ─────────────────────────────────────────────────────
@app.get("/analytics/ticks")
async def get_ticks(
    symbol: str = Query(..., description="Symbol, e.g. BTC/USD"),
    from_ts: Optional[str] = Query(None, alias="from", description="ISO 8601 start"),
    to_ts: Optional[str] = Query(None, alias="to", description="ISO 8601 end"),
    limit: int = Query(1000, ge=1, le=10000),
):
    conn = _get_conn()
    try:
        query = "SELECT ts, symbol, price, volume, side, order_id, trade_id FROM raw_ticks WHERE symbol = %s"
        params: list = [symbol]

        if from_ts:
            query += " AND ts >= %s"
            params.append(from_ts)
        if to_ts:
            query += " AND ts < %s"
            params.append(to_ts)

        query += " ORDER BY ts DESC LIMIT %s"
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        # Serialize UUIDs and timestamps
        result = []
        for r in rows:
            result.append({
                "ts": r["ts"].isoformat() if r["ts"] else None,
                "symbol": r["symbol"],
                "price": float(r["price"]),
                "volume": float(r["volume"]),
                "side": r["side"],
                "order_id": str(r["order_id"]),
                "trade_id": str(r["trade_id"]),
            })
        return result
    finally:
        conn.close()


# ── GET /analytics/ohlcv ─────────────────────────────────────────────────────
@app.get("/analytics/ohlcv")
async def get_ohlcv(
    symbol: str = Query(..., description="Symbol, e.g. BTC/USD"),
    interval: str = Query("1m", description="1m, 5m, 15m, or 1h"),
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
    limit: int = Query(500, ge=1, le=5000),
):
    view = INTERVAL_MAP.get(interval)
    if not view:
        raise HTTPException(status_code=400, detail=f"Invalid interval '{interval}'. Use: 1m, 5m, 15m, 1h")

    conn = _get_conn()
    try:
        # Use format for view name (safe - controlled values only)
        query = f"SELECT bucket, symbol, open, high, low, close, volume FROM {view} WHERE symbol = %s"
        params: list = [symbol]

        if from_ts:
            query += " AND bucket >= %s"
            params.append(from_ts)
        if to_ts:
            query += " AND bucket < %s"
            params.append(to_ts)

        query += " ORDER BY bucket DESC LIMIT %s"
        params.append(limit)

        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

        result = []
        for r in rows:
            result.append({
                "bucket": r["bucket"].isoformat() if r["bucket"] else None,
                "symbol": r["symbol"],
                "open": float(r["open"]) if r["open"] is not None else None,
                "high": float(r["high"]) if r["high"] is not None else None,
                "low": float(r["low"]) if r["low"] is not None else None,
                "close": float(r["close"]) if r["close"] is not None else None,
                "volume": float(r["volume"]) if r["volume"] is not None else None,
            })
        return result
    finally:
        conn.close()


# ── GET /analytics/indicators ────────────────────────────────────────────────
@app.get("/analytics/indicators")
async def get_indicators(
    symbol: str = Query(..., description="Symbol, e.g. BTC/USD"),
    indicator: str = Query(..., description="vwap, sma20, bollinger, or rsi"),
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
):
    conn = _get_conn()
    try:
        now_ts = datetime.now(timezone.utc).isoformat()
        p_from = from_ts or "2000-01-01T00:00:00Z"
        p_to = to_ts or now_ts

        with conn.cursor() as cur:
            if indicator == "vwap":
                cur.execute("SELECT fn_vwap(%s, %s, %s) AS value", (symbol, p_from, p_to))
                row = cur.fetchone()
                return {"indicator": "vwap", "symbol": symbol, "value": float(row["value"]) if row["value"] else None}

            elif indicator == "sma20":
                cur.execute("SELECT fn_sma20(%s, %s) AS value", (symbol, p_to))
                row = cur.fetchone()
                return {"indicator": "sma20", "symbol": symbol, "value": float(row["value"]) if row["value"] else None}

            elif indicator == "bollinger":
                cur.execute("SELECT * FROM fn_bollinger(%s, %s)", (symbol, p_to))
                row = cur.fetchone()
                return {
                    "indicator": "bollinger",
                    "symbol": symbol,
                    "sma20": float(row["sma20"]) if row and row["sma20"] else None,
                    "upper": float(row["upper"]) if row and row["upper"] else None,
                    "lower": float(row["lower"]) if row and row["lower"] else None,
                }

            elif indicator == "rsi":
                cur.execute("SELECT fn_rsi14(%s, %s) AS value", (symbol, p_to))
                row = cur.fetchone()
                return {"indicator": "rsi14", "symbol": symbol, "value": float(row["value"]) if row["value"] else None}

            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown indicator '{indicator}'. Use: vwap, sma20, bollinger, rsi",
                )
    finally:
        conn.close()


# ── POST /analytics/refresh ──────────────────────────────────────────────────
@app.post("/analytics/refresh")
async def trigger_refresh():
    """
    Trigger a background reload of real Kraken trade data into raw_ticks.
    Wipes existing rows and fetches the last 3 days fresh from Kraken REST API.
    Returns immediately; fetch runs in background.
    """
    asyncio.create_task(_auto_fetch_real_data())
    return {"status": "refresh_started", "message": "Fetching 3 days of real Kraken trades in background"}


# ── GET /metrics ─────────────────────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
