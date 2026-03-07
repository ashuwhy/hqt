# API Specification
## Hybrid Trading Database System

**Base URL (via FastAPI proxy):** `http://localhost:8000`  
**Auth:** None required for lab demo (add `X-Client-ID` header for rate-limit tracking)

---

## 1. LOB Engine Endpoints (`/lob`)

### POST `/lob/order`
Place a new limit or market order.

**Request Body:**
```json
{
  "symbol": "BTC-USD",
  "side": "B",
  "order_type": "LIMIT",
  "price": 65000.00,
  "quantity": 0.5,
  "client_id": "trader_001"
}
```

**Response 201:**
```json
{
  "order_id": "uuid-v4",
  "status": "OPEN",
  "ts": "2026-03-08T12:00:00Z",
  "filled_qty": 0
}
```

---

### DELETE `/lob/order/{order_id}`
Cancel an open order.

**Response 200:**
```json
{ "order_id": "uuid-v4", "status": "CANCELLED" }
```

---

### PATCH `/lob/order/{order_id}`
Modify price or quantity of an open order.

**Request Body:**
```json
{ "price": 64900.00, "quantity": 1.0 }
```

---

### GET `/lob/depth/{symbol}`
Return current order book depth snapshot.

**Response 200:**
```json
{
  "symbol": "BTC-USD",
  "ts": "2026-03-08T12:00:00Z",
  "bids": [["65000.00", "1.5"], ["64999.00", "3.2"]],
  "asks": [["65001.00", "0.8"], ["65002.00", "2.1"]]
}
```

---

### WebSocket `ws://localhost:8001/lob/stream/{symbol}`
Live order book updates (depth diffs).

**Message schema:**
```json
{
  "type": "DEPTH_UPDATE",
  "symbol": "BTC-USD",
  "ts": "...",
  "bids_delta": [["65000.00", "0"]],
  "asks_delta": [["65001.00", "1.2"]]
}
```

---

## 2. TimescaleDB Analytics Endpoints (`/analytics`)

### GET `/analytics/ticks`
Query raw tick data.

**Query Params:** `symbol`, `from` (ISO 8601), `to` (ISO 8601), `limit` (default 1000)

**Response 200:** Array of tick objects.

---

### GET `/analytics/ohlcv`
Fetch OHLCV candles.

**Query Params:** `symbol`, `interval` (`1m`|`5m`|`15m`|`1h`), `from`, `to`, `limit`

**Response 200:**
```json
[
  { "bucket": "2026-03-08T12:00:00Z", "symbol": "BTC-USD",
    "open": 65000, "high": 65100, "low": 64950, "close": 65050, "volume": 12.5 }
]
```

---

### GET `/analytics/indicators`
Compute live indicators.

**Query Params:** `symbol`, `indicator` (`vwap`|`sma20`|`bollinger`|`rsi`), `from`, `to`

---

## 3. Graph Endpoints (`/graph`)

### GET `/graph/nodes`
List all currency/asset nodes.

### GET `/graph/edges`
List all directed exchange-rate edges with current weights.

### GET `/graph/paths`
Find all 3-hop cycles from a source node (classical Cypher).

**Query Params:** `from_symbol` (e.g. `USD`), `depth` (default `3`)

---

## 4. Quantum Engine Endpoints (`/quantum`)

### POST `/quantum/run-grover`
Trigger a Grover search on the current graph snapshot.

**Request Body:**
```json
{ "graph_size_n": 16, "method": "QUANTUM" }
```

**Response 200:**
```json
{
  "signal_id": "uuid-v4",
  "path": ["USD", "BTC", "ETH", "USD"],
  "profit_pct": 0.0312,
  "circuit_depth": 47,
  "grover_iterations": 3,
  "quantum_ms": 142.5,
  "classical_ms": 890.2
}
```

### GET `/quantum/signals`
Retrieve recent arbitrage signals.

**Query Params:** `limit` (default 50), `method` (`QUANTUM`|`CLASSICAL`|`ALL`)

---

## 5. Security / Admin Endpoints (`/admin`)

### GET `/admin/security-events`
Retrieve recent security events (SQL injections, rate-limit hits).

**Query Params:** `event_type`, `from`, `to`, `limit`

### GET `/admin/benchmark-runs`
List all past benchmark run summaries.

### POST `/admin/benchmark`
Trigger a Siege-based benchmark programmatically.

---

## 6. Health & Metrics

### GET `/health`
Returns 200 if all services are alive.

```json
{
  "postgres": "ok", "kafka": "ok", "redis": "ok",
  "lob_engine": "ok", "quantum_engine": "ok"
}
```

### GET `/metrics`
Prometheus-format metrics endpoint scraped every 15s.
