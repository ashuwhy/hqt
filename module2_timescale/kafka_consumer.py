"""
Kafka consumer for executed_trades → raw_ticks (TimescaleDB hypertable).

Consumes trade events produced by the C++ LOB engine, batches them,
and bulk-inserts via psycopg3 binary COPY.
"""

import asyncio
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import psycopg
from confluent_kafka import Consumer, KafkaError
from prometheus_client import Counter

logger = logging.getLogger("kafka_consumer")

# ── Prometheus metric ────────────────────────────────────────────────────────
rows_inserted = Counter(
    "timescale_rows_inserted_total",
    "Total rows inserted into raw_ticks",
    ["symbol"],
)

# ── Config ───────────────────────────────────────────────────────────────────
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
TOPIC = "executed_trades"
GROUP_ID = "timescale_ingestor"
BATCH_SIZE = 1000
POLL_TIMEOUT_S = 0.1  # 100ms

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)


def _parse_message(raw: bytes) -> dict | None:
    """Parse a Kafka message from the C++ LOB engine.

    Expected format:
        {"ts":<epoch_ns>,"symbol":"BTC-USD","price":65000.0,
         "qty":0.5,"liquidity_side":"Bid","passive_id":1,"taker_id":2}

    Returns a dict ready for raw_ticks insertion, or None if malformed.
    """
    try:
        data = json.loads(raw)
        ts_ns = data["ts"]
        ts = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
        symbol = data["symbol"]
        price = float(data["price"])
        volume = float(data["qty"])
        liq_side = data.get("liquidity_side", "Bid")
        side = "B" if liq_side == "Bid" else "S"
        order_id = uuid.uuid5(uuid.NAMESPACE_OID, str(data.get("passive_id", 0)))
        trade_id = uuid.uuid5(uuid.NAMESPACE_OID, f"{ts_ns}-{data.get('taker_id', 0)}")
        return {
            "ts": ts,
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "side": side,
            "order_id": order_id,
            "trade_id": trade_id,
        }
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("Malformed message skipped: %s — %s", exc, raw[:200])
        return None


def _bulk_insert(conn: psycopg.Connection, rows: list[dict]) -> int:
    """Binary COPY rows into raw_ticks. Returns count inserted."""
    if not rows:
        return 0
    
    start_time = time.monotonic()
    with conn.cursor() as cur:
        with cur.copy(
            "COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id) "
            "FROM STDIN"
        ) as copy:
            for r in rows:
                copy.write_row((
                    r["ts"],
                    r["symbol"],
                    r["price"],
                    r["volume"],
                    r["side"],
                    r["order_id"],
                    r["trade_id"],
                ))
    conn.commit()
    elapsed = time.monotonic() - start_time
    logger.info(
        "Inserted batch of %d rows in %.3fs (%.1f rows/s)",
        len(rows), elapsed, len(rows) / max(elapsed, 0.001)
    )
    return len(rows)


def _verify_hypertable(conn: psycopg.Connection) -> None:
    """Log hypertable status on startup."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM timescaledb_information.chunks "
            "WHERE hypertable_name = 'raw_ticks'"
        )
        chunk_count = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM raw_ticks")
        row_count = cur.fetchone()[0]
        logger.info(
            "raw_ticks hypertable verified — %d chunks, %d rows",
            chunk_count,
            row_count,
        )


async def run_consumer() -> None:
    """Main consumer loop — runs forever as a background asyncio task."""
    logger.info("Starting Kafka consumer on topic=%s group=%s", TOPIC, GROUP_ID)

    # ── Retry loop for Kafka connection ──────────────────────────────────────
    consumer = None
    retry_count = 0
    while True:
        try:
            consumer = Consumer({
                "bootstrap.servers": KAFKA_BOOTSTRAP,
                "group.id": GROUP_ID,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": True,
            })
            # Test connection with list_topics
            consumer.list_topics(timeout=5.0)
            break
        except Exception as exc:
            retry_count += 1
            logger.warning("Kafka not ready (retry %d): %s. Retrying in 5s...", retry_count, exc)
            if consumer:
                consumer.close()
            await asyncio.sleep(5)

    consumer.subscribe([TOPIC])
    assert consumer is not None

    # ── Retry loop for DB connection ─────────────────────────────────────────
    conn = None
    while True:
        try:
            conn = psycopg.connect(PG_DSN, autocommit=False)
            _verify_hypertable(conn)
            break
        except Exception as exc:
            logger.warning("DB not ready: %s. Retrying in 5s...", exc)
            await asyncio.sleep(5)
    
    assert conn is not None

    batch: list[dict] = []

    try:
        while True:
            msg = consumer.poll(timeout=POLL_TIMEOUT_S)

            if msg is None:
                # Timeout — flush whatever we have
                if batch:
                    inserted = _bulk_insert(conn, batch)
                    for r in batch:
                        rows_inserted.labels(symbol=r["symbol"]).inc()
                    logger.debug("Flushed %d rows (timeout)", inserted)
                    batch.clear()
                await asyncio.sleep(0)  # yield to event loop
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                logger.error("Kafka error: %s", msg.error())
                await asyncio.sleep(1)
                continue

            parsed = _parse_message(msg.value())
            if parsed:
                batch.append(parsed)

            if len(batch) >= BATCH_SIZE:
                inserted = _bulk_insert(conn, batch)
                for r in batch:
                    rows_inserted.labels(symbol=r["symbol"]).inc()
                logger.debug("Flushed %d rows (batch full)", inserted)
                batch.clear()

            await asyncio.sleep(0)  # yield to event loop
    except asyncio.CancelledError:
        logger.info("Consumer task cancelled, flushing remaining %d rows", len(batch))
        if batch:
            _bulk_insert(conn, batch)
    finally:
        consumer.close()
        conn.close()
        logger.info("Kafka consumer shut down")
