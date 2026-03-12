"""
Live trade streamer from Kraken public WebSocket → Kafka executed_trades topic.

Kraken's WebSocket API is free, requires no API key, and has no geo-restrictions.
This streams real trades and publishes them to Kafka in the same format as the
C++ LOB engine, so the existing kafka_consumer.py picks them up.

Usage:
    python -m module2_timescale.live_streamer --symbols BTC/USD,ETH/USD --duration 300

Requires: pip install websockets
"""

import argparse
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("live_streamer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
KRAKEN_WS_URL = os.getenv("KRAKEN_WS_URL", "wss://ws.kraken.com/v2")

# Kraken pair names → our symbol format
KRAKEN_PAIRS = {
    "BTC/USD": "BTC/USD",
    "ETH/USD": "ETH/USD",
    "SOL/USD": "SOL/USD",
    "XRP/USD": "XRP/USD",
    "ADA/USD": "ADA/USD",
    "DOT/USD": "DOT/USD",
    "DOGE/USD": "DOGE/USD",
    "AVAX/USD": "AVAX/USD",
    "MATIC/USD": "MATIC/USD",
}


def _make_lob_format(symbol: str, price: float, qty: float, side: str, ts_epoch_ns: int, trade_id: int) -> str:
    """Format a trade in the same JSON schema as the C++ LOB engine.

    This way the existing kafka_consumer.py can ingest it without changes.
    """
    return json.dumps({
        "ts": ts_epoch_ns,
        "symbol": symbol,
        "price": price,
        "qty": qty,
        "liquidity_side": "Bid" if side == "b" else "Ask",
        "passive_id": trade_id,
        "taker_id": trade_id + 1,
    })


async def stream_trades(symbols: list[str], duration_sec: int) -> None:
    """Connect to Kraken WebSocket and stream real trades to Kafka."""
    try:
        import websockets
    except ImportError:
        logger.error("websockets package not installed. Run: pip install websockets")
        return

    try:
        from confluent_kafka import Producer
    except ImportError:
        logger.error("confluent-kafka not installed")
        return

    producer = Producer({"bootstrap.servers": KAFKA_BOOTSTRAP})
    trade_count = 0
    start_time = time.monotonic()

    # Map requested symbols to Kraken pair format
    kraken_symbols = []
    for sym in symbols:
        if sym in KRAKEN_PAIRS:
            kraken_symbols.append(sym)
        else:
            logger.warning("Symbol %s not available on Kraken, skipping", sym)

    if not kraken_symbols:
        logger.error("No valid Kraken symbols to subscribe to")
        return

    logger.info("Connecting to Kraken WebSocket for %s (duration=%ds)...", kraken_symbols, duration_sec)

    async with websockets.connect(KRAKEN_WS_URL) as ws:
        # Subscribe to trade channel
        subscribe_msg = {
            "method": "subscribe",
            "params": {
                "channel": "trade",
                "symbol": kraken_symbols,
            }
        }
        await ws.send(json.dumps(subscribe_msg))
        logger.info("Subscribed to trade channel for %s", kraken_symbols)

        try:
            while (time.monotonic() - start_time) < duration_sec:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

                msg = json.loads(raw)

                # Skip heartbeats and subscription confirmations
                if msg.get("channel") != "trade":
                    continue

                for trade in msg.get("data", []):
                    symbol = trade.get("symbol", "")
                    price = float(trade.get("price", 0))
                    qty = float(trade.get("qty", 0))
                    side = trade.get("side", "b")
                    ts_str = trade.get("timestamp", "")

                    # Parse Kraken's ISO timestamp to epoch nanoseconds
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        ts_ns = int(dt.timestamp() * 1e9)
                    except (ValueError, AttributeError):
                        ts_ns = int(time.time() * 1e9)

                    trade_count += 1

                    # Convert to LOB-compatible format
                    payload = _make_lob_format(symbol, price, qty, side, ts_ns, trade_count)
                    producer.produce("executed_trades", value=payload.encode())

                    if trade_count % 100 == 0:
                        producer.flush()
                        elapsed = time.monotonic() - start_time
                        logger.info(
                            "Streamed %d real trades (%.0fs elapsed, %.1f trades/s)",
                            trade_count, elapsed, trade_count / elapsed,
                        )

        except asyncio.CancelledError:
            pass

    producer.flush()
    elapsed = time.monotonic() - start_time
    logger.info(
        "Done: %d real trades streamed in %.0fs (%.1f trades/s)",
        trade_count, elapsed, trade_count / max(elapsed, 0.001),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream real trades from Kraken → Kafka")
    parser.add_argument("--symbols", type=str, default="BTC/USD,ETH/USD",
                        help="Comma-separated Kraken pairs")
    parser.add_argument("--duration", type=int, default=300,
                        help="Seconds to stream (default: 300 = 5 min)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    asyncio.run(stream_trades(symbols, args.duration))


if __name__ == "__main__":
    main()
