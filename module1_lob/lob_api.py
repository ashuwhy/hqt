import os
import json
import uuid
import time
from datetime import datetime, timezone
import threading
import asyncio
from typing import Dict, List, Optional
from collections import deque

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
import psycopg
from confluent_kafka import Consumer

from prometheus_client import Counter, Histogram, Gauge, generate_latest, CONTENT_TYPE_LATEST

import olob
from module1_lob.ring_buffer import RingBuffer

app = FastAPI(title="HQT LOB Engine")

# --- Globals ---
_event_loop = None
books: Dict[str, olob.Book] = {}
ring_buffer = RingBuffer(2**20)

trade_event_buffer = deque()
trade_event_lock = threading.Lock()
trade_event_cv = threading.Condition(trade_event_lock)

active_websockets: Dict[str, List[WebSocket]] = {}

# --- Config ---
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "hqt")
POSTGRES_PASS = os.environ.get("POSTGRES_PASSWORD", "hqt_secret")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "hqt")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
DSN = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASS}@{POSTGRES_HOST}:5432/{POSTGRES_DB}"

# --- Metrics ---
lob_orders_total = Counter('lob_orders_total', 'Total orders', ['symbol', 'side'])
lob_trades_total = Counter('lob_trades_total', 'Total trades', ['symbol'])
lob_order_latency_ms = Histogram('lob_order_latency_ms', 'Order latency ms')
lob_active_orders = Gauge('lob_active_orders', 'Active orders', ['symbol'])

# --- Models ---
class OrderReq(BaseModel):
    symbol: str
    side: str
    order_type: str
    price: Optional[float] = 0.0
    quantity: float
    client_id: Optional[str] = None

class ModifyReq(BaseModel):
    price: float
    quantity: float

def get_book(symbol: str) -> olob.Book:
    if symbol not in books:
        books[symbol] = olob.Book()
    return books[symbol]

# Async push helper
def broadcast_depth(symbol: str):
    if symbol in active_websockets and len(active_websockets[symbol]) > 0:
        depth = get_book(symbol).l2(10)
        msg = json.dumps({"type": "DEPTH_UPDATE", "symbol": symbol, "bids": depth["bids"], "asks": depth["asks"]})
        
        async def send(ws, m):
            try:
                await ws.send_text(m)
            except Exception:
                pass
                
        for ws in active_websockets[symbol]:
            if _event_loop is not None:
                asyncio.run_coroutine_threadsafe(send(ws, msg), _event_loop)

# --- Threads ---

def inbound_thread():
    consumer = Consumer({
        'bootstrap.servers': KAFKA_BOOTSTRAP,
        'group.id': 'lob_inbound_group',
        'auto.offset.reset': 'latest',
        'enable.auto.commit': True
    })
    consumer.subscribe(['raw_orders'])
    print("InboundThread initialized.")
    while True:
        msg = consumer.poll(0.1)
        if msg is None or msg.error():
            continue
        try:
            data = json.loads(msg.value().decode('utf-8'))
            if "ts" not in data:
                data["ts"] = time.time()
            ring_buffer.publish(data)
        except Exception as e:
            pass

def matching_thread():
    seq = 0
    print("MatchingThread initialized.")
    while True:
        pub_seq = ring_buffer.get_published_seq()
        if seq <= pub_seq:
            batch = []
            while seq <= pub_seq:
                event = ring_buffer[seq]
                batch.append(event)
                seq += 1
            
            ring_buffer.set_matching_seq(seq - 1)
            
            for ev in batch:
                action = ev.get("action")
                sym = ev.get("symbol")
                bk = get_book(sym)
                
                # Convert string UUID to 64-bit int for olob order ID
                # (olob requires uint64_t for OrderId). We fake it with hash.
                oid = hash(ev.get("order_id", str(uuid.uuid4()))) & 0xFFFFFFFFFFFFFFFF
                
                if action == "PLACE":
                    start_t = time.perf_counter()
                    o = olob.NewOrder()
                    o.id = oid
                    o.side = olob.Side.Bid if ev["side"] == "B" else olob.Side.Ask
                    # Int price tick = val * 1e8
                    o.price = int(float(ev["price"]) * 100000000)
                    o.qty = int(float(ev["qty"]) * 100000000)
                    o.ts = int(time.time() * 1e9)
                    
                    if ev["type"] == "LIMIT":
                        bk.submit_limit(o)
                    else:
                        bk.submit_market(o)
                        
                    latency = (time.perf_counter() - start_t) * 1000.0
                    lob_order_latency_ms.observe(latency)
                    lob_orders_total.labels(symbol=sym, side=ev["side"]).inc()
                    
                elif action == "CANCEL":
                    bk.cancel(oid)
                elif action == "MODIFY":
                    m_ev = olob.ModifyOrder()
                    m_ev.id = oid
                    m_ev.new_price = int(float(ev["price"]) * 100000000)
                    m_ev.new_qty = int(float(ev["qty"]) * 100000000)
                    bk.modify(m_ev)
                
                # Poll trades exposed by our PyLogger modification
                trades = bk.poll_trades()
                if trades:
                    with trade_event_cv:
                        for t in trades:
                            t["symbol"] = sym
                            trade_event_buffer.append(t)
                        trade_event_cv.notify()
                    lob_trades_total.labels(symbol=sym).inc(len(trades))
                    # depth stream
                    broadcast_depth(sym)
                    
        else:
            time.sleep(0.0001)

def persistence_thread():
    print("PersistenceThread initialized.")
    while True:
        trades_to_copy = []
        with trade_event_cv:
            trade_event_cv.wait(timeout=0.1)
            while trade_event_buffer:
                trades_to_copy.append(trade_event_buffer.popleft())
                
        if not trades_to_copy:
            continue
            
        try:
            with psycopg.connect(DSN) as conn:
                with conn.cursor() as cur:
                    with cur.copy("COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id) FROM STDIN") as copy1:
                        for t in trades_to_copy:
                            dt = datetime.fromtimestamp(t["ts"]/1e9, tz=timezone.utc)
                            ts_val = dt.strftime('%Y-%m-%d %H:%M:%S.%f%z')
                            price = t["price"] / 100000000.0
                            qty = t["qty"] / 100000000.0
                            side = 'B' if t["liquidity_side"] == "Bid" else 'S'
                            uid_order = str(uuid.uuid4())
                            uid_trade = str(uuid.uuid4())
                            copy1.write_row((ts_val, t["symbol"], price, qty, side, uid_order, uid_trade))

                    with cur.copy("COPY trades (trade_id, ts, symbol, buy_order_id, sell_order_id, price, quantity) FROM STDIN") as copy2:
                        for t in trades_to_copy:
                            dt = datetime.fromtimestamp(t["ts"]/1e9, tz=timezone.utc)
                            ts_val = dt.strftime('%Y-%m-%d %H:%M:%S.%f%z')
                            price = t["price"] / 100000000.0
                            qty = t["qty"] / 100000000.0
                            uid_trade = str(uuid.uuid4()) # In reality link this properly
                            buy_id = str(uuid.uuid4())
                            sell_id = str(uuid.uuid4())
                            copy2.write_row((uid_trade, ts_val, t["symbol"], buy_id, sell_id, price, qty))
                conn.commit()
                
            # Set persist seq just roughly (it's not tracking per-order accuracy in this simplified version)
            ring_buffer.set_persist_seq(ring_buffer.get_matching_seq())
            
        except Exception as e:
            print(f"Error persisting trades: {e}")
            time.sleep(1)

# --- App Lifecycle ---
@app.on_event("startup")
def startup():
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    threading.Thread(target=inbound_thread, daemon=True).start()
    threading.Thread(target=matching_thread, daemon=True).start()
    threading.Thread(target=persistence_thread, daemon=True).start()

# --- Endpoints ---
@app.post("/lob/order")
def place_order(req: OrderReq):
    oid = str(uuid.uuid4())
    ev = {
        "action": "PLACE",
        "order_id": oid,
        "symbol": req.symbol,
        "side": req.side,
        "type": req.order_type,
        "price": req.price,
        "qty": req.quantity,
        "ts": time.time()
    }
    ring_buffer.publish(ev)
    return {"status": "success", "order_id": oid}

@app.delete("/lob/order/{order_id}")
def cancel_order(order_id: str, symbol: str):
    ev = {"action": "CANCEL", "order_id": order_id, "symbol": symbol}
    ring_buffer.publish(ev)
    return {"status": "CANCELLED"}

@app.patch("/lob/order/{order_id}")
def modify_order(order_id: str, symbol: str, req: ModifyReq):
    ev = {
        "action": "MODIFY",
        "order_id": order_id,
        "symbol": symbol,
        "price": req.price,
        "qty": req.quantity
    }
    ring_buffer.publish(ev)
    return {"status": "MODIFIED"}

@app.get("/lob/depth/{symbol}")
def get_depth(symbol: str, levels: int = 10):
    bk = get_book(symbol)
    depth = bk.l2(levels)
    # Convert back to float
    res_bids = [[px / 1e8, float(qty) / 1e8] for px, qty in depth["bids"]]
    res_asks = [[px / 1e8, float(qty) / 1e8] for px, qty in depth["asks"]]
    return {"bids": res_bids, "asks": res_asks}

@app.websocket("/lob/stream/{symbol}")
async def stream_depth(websocket: WebSocket, symbol: str):
    await websocket.accept()
    if symbol not in active_websockets:
        active_websockets[symbol] = []
    active_websockets[symbol].append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        active_websockets[symbol].remove(websocket)

@app.get("/lob/health")
def health():
    return {"status": "OK", "active_symbols": list(books.keys())}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
