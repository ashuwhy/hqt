"""
Kraken WebSocket Feeder for LOB Engine.

This script connects to the Kraken WebSocket API (v2) for real-time L2 order book (depth)
and feeds those bids and asks as synthetic limit orders into our internal C++ LOB engine.

"""

import asyncio
import json
import logging
import time
import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("kraken_feeder")

KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
LOB_API_URL = "http://localhost:8001/lob/order"

# Kraken pair names → our LOB symbol format
SYMBOLS = {
    "BTC/USD": "BTCUSD",
    "ETH/USD": "ETHUSD",
    "LINK/USD": "LINKUSD",
    "SOL/USD": "SOLUSD",
    "ADA/USD": "ADAUSD",
    "XRP/USD": "XRPUSD",
    "DOGE/USD": "DOGEUSD",
    "AVAX/USD": "AVAXUSD",
    "UNI/USD": "UNIUSD",
    "DOT/USD": "DOTUSD",
}

async def send_order(client: httpx.AsyncClient, symbol: str, side: str, price: float, qty: float):
    """Send a limit order to the internal LOB engine."""
    payload = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": qty,
        "ordertype": "LIMIT"
    }
    try:
        r = await client.post(LOB_API_URL, json=payload, timeout=2.0)
        from_kraken = "Bid" if side == "B" else "Ask"
        if r.status_code != 201:
            logger.warning(f"Failed to insert {from_kraken} {qty} {symbol} @ {price}: {r.text}")
    except Exception as e:
        logger.error(f"Error submitting order: {e}")

async def run_feeder():
    kraken_pairs = list(SYMBOLS.keys())
    
    logger.info(f"Connecting to Kraken WebSocket for {kraken_pairs}...")
    
    async with httpx.AsyncClient() as http_client:
        while True:
            try:
                async with websockets.connect(KRAKEN_WS_URL) as ws:
                    # Subscribe to L2 book (depth 10 is enough to get a healthy spread)
                    sub_msg = {
                        "method": "subscribe",
                        "params": {
                            "channel": "book",
                            "depth": 10,
                            "symbol": kraken_pairs
                        }
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info("Subscribed to L2 order book.")
                    
                    last_update = time.time()
                    orders_sent = 0

                    while True:
                        msg = await ws.recv()
                        data = json.loads(msg)
                        
                        if data.get("channel") != "book":
                            continue
                            
                        # Format is either snapshot or update
                        updates = data.get("data", [])
                        for update in updates:
                            symbol = update.get("symbol")
                            if symbol not in SYMBOLS:
                                continue
                            
                            lob_sym = SYMBOLS[symbol]
                            
                            # Fire and forget concurrent orders to avoid blocking WS receiver
                            tasks = []
                            
                            for bid in update.get("bids", []):
                                price, qty = float(bid["price"]), float(bid["qty"])
                                # On Kraken, qty=0 means level removed. LOB engine doesn't support cancel yet,
                                # so we just ignore 0 quantities.
                                if qty > 0:
                                    tasks.append(send_order(http_client, lob_sym, "B", price, qty))
                                    orders_sent += 1
                                    
                            for ask in update.get("asks", []):
                                price, qty = float(ask["price"]), float(ask["qty"])
                                if qty > 0:
                                    tasks.append(send_order(http_client, lob_sym, "A", price, qty))
                                    orders_sent += 1
                                    
                            if tasks:
                                for t in tasks:
                                    asyncio.create_task(t)
                                
                        if time.time() - last_update > 5:
                            logger.info(f"Sent {orders_sent} orders to LOB in last 5s")
                            orders_sent = 0
                            last_update = time.time()
                            
            except (asyncio.CancelledError, KeyboardInterrupt):
                logger.info("Feeder stopped.")
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}. Reconnecting in 3s...")
                await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(run_feeder())
