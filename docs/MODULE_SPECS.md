# Module Implementation Specifications

## Hybrid Trading Database System

---

## Module 1 – High-QPS Limit Order Book Engine

**Owner:** Member 1 (Ashutosh Sharma)
**Language:** C++20 engine + uWebSockets + librdkafka

### Core Engine Integration

- **Engine provider:** Git Submodule `https://github.com/ashuwhy/lob` at `module1_lob/engine/`
- **Native C++:** The network layer and matching engine are compiled together into a single C++ binary `lob_server`.
- **Rule:** Do NOT rewrite the C++ matching engine itself (`book_core.cpp`). Wrap it with a high-performance network layer.

### Data Structures (C++ side)

| Structure | Purpose | Complexity |
|-----------|---------|-----------|
| Red-Black Tree (`book_core.cpp`) | Price-level index, bid and ask sides | O(log M) insert/delete |
| Per-price FIFO deque | Price-time priority matching | O(1) match/cancel |
| `mempool.hpp` arena allocator | Lock-free order memory allocation | O(1) alloc |
| Lock-free Queue / Ring Buffer | Inter-thread communication | Lock-free inbound/outbound |

### C++ Threading Model

```cpp
// Thread A — Inbound (Kafka Consumer)
// Consumes 'raw_orders' via librdkafka.
// Pushes parsed Order to inbound_queue.

// Thread B — Matching
// Pops from inbound_queue.
// Calls book.place(order).
// Pushes resulting Trades to outbound_queue.
// Broadcasts depth updates via uWebSockets.

// Thread C — Outbound (Kafka Producer)
// Pops from outbound_queue.
// Publishes to 'executed_trades' via librdkafka.
```

### FastAPI Endpoints

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/lob/order` | Add limit/market order → publish to Kafka internally → HTTP 201 |
| `DELETE` | `/lob/order/{order_id}` | Cancel order |
| `PATCH` | `/lob/order/{order_id}` | Atomic cancel + re-place at new price/qty |
| `GET` | `/lob/depth/{symbol}` | top-10 bids/asks JSON |
| `WS` | `/lob/stream/{symbol}` | Async WebSocket broadcast per match |
| `GET` | `/lob/health` | Active symbols + status; used by docker healthcheck |
| `GET` | `/lob/metrics` | Prometheus exposition |

### Prometheus Metrics

```cpp
// Exposed via prometheus-cpp
Family<Counter>& lob_orders_total;    // labels: symbol, side
Family<Counter>& lob_trades_total;    // labels: symbol
Family<Histogram>& lob_order_latency; // latency inside the matching engine
Family<Gauge>& lob_active_orders;     // labels: symbol
```

### Network Layer Performance Tiers

| Tier | Approach | Est. QPS | Status |
|------|----------|---------|--------|
| 1 | C++ uWebSockets (`lob_api.cpp`) — packets arrive directly into C++ memory | 100,000+ | **Implementing now** |
| 2 | Persistent WebSocket / binary msgpack frames | 10,000+ | Future optimisation |
| 3 | Python FastAPI + orjson + batch endpoint | 5,000+ | Deprecated / Replaced |

We have pivoted from Tier 3 (Python) directly to Tier 1 (C++ Native) to achieve HFT latency.

### Benchmarking

```bash
# 1. Siege — single-order baseline
siege -c 200 -t 30S --content-type "application/json" -f module1_lob/urls.txt

# Create siege.conf: content-type = application/json
```

Target: > 100,000 order ops/sec at p99 < 10ms. Record in `benchmark_runs`.

---

## Module 2 – TimescaleDB Temporal Analytics Engine

**Owner:** Member 2 (Sujal Anil Kaware)
**Language:** Python 3.12

### Data Ingestion Pipeline

1. `kafka_consumer.py` — `confluent_kafka.Consumer` on `executed_trades` (instead of `raw_orders`), group_id `timescale_ingestor`
2. Batches of 1,000 records or 100ms timeout → `psycopg3` binary COPY into `raw_ticks`
3. `gen_ticks.py` — GBM price series (`dS = S * (μ dt + σ dW)`, μ=0, σ=0.02), 1M rows, 10 symbols
4. On startup: verify `raw_ticks` hypertable exists, log chunk count

### Hypertable Configuration

- Chunk interval: 1 day
- Space partitioning: 4 partitions by `symbol` hash
- Compression after 7 days (`compress_segmentby = 'symbol'`)
- Retention: drop chunks older than 90 days

### Continuous Aggregates (all 4 — defined in `init.sql`)

| View | Bucket | Refresh interval | Refresh offset |
|------|--------|-----------------|----------------|
| `ohlcv_1m` | 1 minute | 1 minute | 1 minute |
| `ohlcv_5m` | 5 minutes | 5 minutes | 5 minutes |
| `ohlcv_15m` | 15 minutes | 15 minutes | 15 minutes |
| `ohlcv_1h` | 1 hour | 1 hour | 1 hour |

### SQL Technical Indicators (`indicators.sql`)

```sql
-- VWAP
CREATE OR REPLACE FUNCTION fn_vwap(p_symbol TEXT, p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS NUMERIC AS $$
  SELECT SUM(price * volume) / NULLIF(SUM(volume), 0)
  FROM raw_ticks
  WHERE symbol = p_symbol AND ts BETWEEN p_from AND p_to;
$$ LANGUAGE sql STABLE;

-- SMA-20
CREATE OR REPLACE FUNCTION fn_sma20(p_symbol TEXT, p_at TIMESTAMPTZ)
RETURNS NUMERIC AS $$
  SELECT AVG(close) FROM (
    SELECT close FROM ohlcv_1m
    WHERE symbol = p_symbol AND bucket <= p_at
    ORDER BY bucket DESC LIMIT 20
  ) t;
$$ LANGUAGE sql STABLE;

-- RSI-14 (inline query via fn_rsi14 wrapper)
WITH gains_losses AS (
  SELECT bucket, symbol, close,
    GREATEST(close - LAG(close) OVER (PARTITION BY symbol ORDER BY bucket), 0) AS gain,
    GREATEST(LAG(close) OVER (PARTITION BY symbol ORDER BY bucket) - close, 0) AS loss
  FROM ohlcv_1m
), avg_gl AS (
  SELECT bucket, symbol, close,
    AVG(gain) OVER (PARTITION BY symbol ORDER BY bucket ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain,
    AVG(loss) OVER (PARTITION BY symbol ORDER BY bucket ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss
  FROM gains_losses
)
SELECT bucket, symbol, 100 - (100 / (1 + avg_gain / NULLIF(avg_loss, 0))) AS rsi_14
FROM avg_gl;
```

### Analytics API (`analytics_api.py`)

```
GET /analytics/ticks?symbol=&from=&to=&limit=1000
GET /analytics/ohlcv?symbol=&interval=1m|5m|15m|1h&from=&to=
GET /analytics/indicators?symbol=&indicator=vwap|sma20|bollinger|rsi&from=&to=
GET /analytics/health  → {"status":"ok","row_count":<int>}
```

Mounted into `module5_security/main.py` via `app.include_router(analytics_router, prefix="/analytics")`.

### Benchmark (`bench_timescale.py`)

- Create `raw_ticks_plain` (same schema, no hypertable)
- Load same 1M rows into both tables
- Run same OHLCV range query 10× on each; record avg + p99
- Write to `benchmark_runs` table + save `benchmark_timescale.csv` + `benchmark_timescale.png`
- Expected: hypertable ≥ 10× faster on time-range queries at 1M rows

---

## Module 3 – Graph Database Layer + Primary Arbitrage Engine

**Owner:** Member 3 (Parag Mahadeo Chimankar)
**Language:** Python 3.12 + Cypher (Apache AGE)

> **Architecture Decision (ADR, March 9 2026):** Bellman-Ford is the production arbitrage algorithm. It runs every 500ms, deterministically finds all profitable cycles, and writes `method='CLASSICAL'` signals. Quantum (Module 4) uses this module's `/graph/rates` output for benchmarking only.

### Graph Schema

- **Nodes:** 20 `Asset` vertices — BTC ETH BNB SOL ADA XRP DOGE AVAX MATIC DOT + USD EUR GBP JPY AUD CAD CHF INR SGD HKD
- **Edges:** Directed `EXCHANGE` edges for all active trading pairs
- **Edge properties:** `bid FLOAT`, `ask FLOAT`, `spread FLOAT`, `last_updated BIGINT`

### Initialization (`graph_init.py`)

Uses `MERGE` for idempotency. Acceptance criteria:

```sql
SELECT * FROM cypher('fx_graph', $$ MATCH (n:Asset) RETURN count(n) $$) AS (c agtype);
-- Must return 20
SELECT * FROM cypher('fx_graph', $$ MATCH ()-[r:EXCHANGE]->() RETURN count(r) $$) AS (c agtype);
-- Must return ≥ 50
```

### Edge Weight Updater (`edge_weight_updater.py`)

```python
async def update_edge_weights():
    while True:
        for symbol in ACTIVE_PAIRS:
            try:
                depth = await lob_client.get(f"http://lob-engine:8001/lob/depth/{symbol}")
                best_bid = depth["bids"][0][0]
                best_ask = depth["asks"][0][0]
                # Cypher UPDATE via psycopg3 + AGE
            except Exception as e:
                logger.warning(f"LOB unavailable for {symbol}: {e} — skipping")
        await asyncio.sleep(0.5)
```

Prometheus gauge: `graph_edge_update_lag_ms`

### Bellman-Ford — Primary Arbitrage Engine (`bellman_ford.py`)

```python
import math

def build_rate_matrix(conn) -> dict:
    """Query all EXCHANGE edges from AGE graph."""
    # Returns {(from_sym, to_sym): rate, ...}

def bellman_ford_arbitrage(rates_matrix: dict, nodes: list) -> list | None:
    """Weight transform: w = -log(rate). Negative cycle = profitable arbitrage."""
    dist = {n: float('inf') for n in nodes}
    dist[nodes[0]] = 0
    predecessor = {}
    for _ in range(len(nodes) - 1):
        for (u, v), rate in rates_matrix.items():
            w = -math.log(rate)
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                predecessor[v] = u
    # Nth pass: detect negative cycle
    for (u, v), rate in rates_matrix.items():
        if dist[u] + (-math.log(rate)) < dist[v]:
            return extract_cycle(predecessor, v)
    return None

def extract_cycle(predecessor: dict, start: str) -> list[str]:
    """Walk predecessor map to build full cycle path."""

def benchmark_bellman_ford(n_nodes: int, n_trials: int) -> dict:
    """Returns timing stats for comparison with Grover in Module 4."""
```

### Cypher Query Functions (`graph_queries.py`)

1. `find_3hop_arbitrage_cycles(from_symbol)` — all profitable directed 3-hop cycles from a node
2. `find_shortest_path(from_sym, to_sym)` — most profitable exchange route between two assets
3. `find_high_spread_edges(threshold)` — edges where `spread > threshold`
4. `crypto_subgraph()` — subgraph of crypto-only Asset nodes

### Graph API (`graph_api.py`)

```
GET /graph/nodes              → list all Asset vertices
GET /graph/edges              → list EXCHANGE edges with current bid/ask
GET /graph/paths?from_symbol= → 3-hop Bellman-Ford cycle search
GET /graph/rates              → N×N adjacency matrix JSON (consumed by Module 4)
GET /graph/health             → node count + last edge update timestamp
```

Mounted into `module5_security/main.py` via `app.include_router(graph_router, prefix="/graph")`.

---

## Module 4 – Quantum Arbitrage Detection Engine *(Research Benchmark)*

**Owner:** Member 4 (Kshetrimayum Abo)
**Language:** Python 3.12 + Qiskit 0.45 + qiskit-aer 0.13

> **Architecture Decision (ADR, March 9 2026):** Quantum does NOT replace Bellman-Ford. It runs on the same rate matrix from `/graph/rates`, its wall-clock time is recorded alongside Bellman-Ford's, and the final report shows the O(√N) vs O(N) complexity comparison. AerSimulator is *slower* than Bellman-Ford due to classical state-vector overhead — this is the **expected, documented** result.

### Immediate Priority: Add `/health` Stub

`quantum_api.py` must expose `GET /health → {"status":"ok"}` **first** before any other work. This unblocks `fastapi-proxy` startup (which was stuck on `service_healthy` for a placeholder container).

### Qiskit Pipeline

**State Encoding**

```python
# All P(N,3) = N!/(N-3)! directed 3-cycles as basis states
n_qubits = math.ceil(math.log2(len(all_cycles)))
# N=8  → 336 cycles → 9 qubits
# N=16 → 3360 cycles → 12 qubits
# N=32 → 29760 cycles → 15 qubits
# Cap at N=64 maximum (beyond this AerSimulator exhausts RAM)
```

**Oracle (`grover_oracle.py`)**

```python
def build_oracle(profitable_states: list[int], n_qubits: int) -> QuantumCircuit:
    for state in profitable_states:
        binary = format(state, f'0{n_qubits}b')
        for i, bit in enumerate(reversed(binary)):
            if bit == '0': qc.x(qr[i])
        qc.h(qr[-1])
        qc.mcx(list(range(n_qubits - 1)), n_qubits - 1)
        qc.h(qr[-1])
        for i, bit in enumerate(reversed(binary)):
            if bit == '0': qc.x(qr[i])
    return qc
```

**Diffuser (`grover_diffuser.py`)**

```python
def build_diffuser(n_qubits: int) -> QuantumCircuit:
    qc.h(range(n_qubits)); qc.x(range(n_qubits))
    qc.h(n_qubits - 1); qc.mcx(list(range(n_qubits - 1)), n_qubits - 1)
    qc.h(n_qubits - 1); qc.x(range(n_qubits)); qc.h(range(n_qubits))
    return qc
# Verify: unitary matrix == 2|s><s| - I for N=2
```

**Full Grover Run (`run_grover.py`)**

```python
def run_grover(rates_matrix, nodes, shots=1024) -> dict:
    cycles = enumerate_cycles(nodes, k=3)
    profitable = [i for i, c in enumerate(cycles) if is_profitable(c, rates_matrix)]
    n_qubits = math.ceil(math.log2(len(cycles)))
    n_iter = max(1, int(math.pi / 4 * math.sqrt(len(cycles) / max(len(profitable), 1))))
    qc = QuantumCircuit(n_qubits, n_qubits)
    qc.h(range(n_qubits))
    for _ in range(n_iter):
        qc.compose(build_oracle(profitable, n_qubits), inplace=True)
        qc.compose(build_diffuser(n_qubits), inplace=True)
    qc.measure(range(n_qubits), range(n_qubits))
    result = AerSimulator().run(qc, shots=shots).result()
    best_state = int(max(result.get_counts(), key=result.get_counts().get), 2)
    return {"path": cycles[best_state], "circuit_depth": qc.depth(), ...}
```

### Background Service (`quantum_service.py`)

```python
# Runs every 10 seconds:
# 1. GET /graph/rates → fetch live N×N rate matrix
# 2. Run bellman_ford_arbitrage()  → record classical_ms
# 3. Run run_grover()              → record quantum_ms
# 4. INSERT both into arbitrage_signals (method='CLASSICAL' and method='QUANTUM')
```

Prometheus histograms: `quantum_grover_ms`, `quantum_bellman_ford_ms`

### Benchmark (`benchmark_quantum.py`)

- N ∈ {4, 8, 12, 16, 20, 24, 28, 32} nodes
- 10 trials per method per N on synthetic random rate matrix
- Records: `n_nodes, bellman_ford_ms_avg, bellman_ford_ms_p99, grover_ms_avg, grover_ms_p99, n_qubits, circuit_depth, grover_iterations`
- Output: `benchmark_quantum.csv` + `benchmark_quantum.png` (dual line chart, log-scale Y, O(N) and O(√N) reference lines)
- Report narrative: "Bellman-Ford is O(N·E) and completes in < 5ms for all tested graph sizes. Grover's Algorithm has a theoretical O(√N) query complexity advantage over classical search, but this advantage applies only to oracle queries on real quantum hardware — AerSimulator computes the full state vector classically, resulting in exponential time overhead as N grows. The benchmark demonstrates the theoretical complexity, not a practical speedup."

### Quantum API (`quantum_api.py`)

```
GET  /health                    → {"status":"ok"}  ← IMPLEMENT FIRST
POST /quantum/run-grover        body: {graph_size_n, method}
GET  /quantum/signals           ?limit=50&method=QUANTUM|CLASSICAL|ALL
GET  /quantum/benchmark         → latest benchmark_quantum.csv rows as JSON
GET  /metrics                   → Prometheus exposition
```

---

## Module 5 – Security, Observability & DoS Prevention

**Owner:** Member 5 (Kartik Pandey)
**Language:** Python 3.12 + FastAPI + Redis + Prometheus

### Application Entry Point (`main.py`)

```python
from fastapi import FastAPI
# Module routers (same process for analytics + graph; reverse proxy for lob + quantum)
app.include_router(analytics_router, prefix="/analytics")
app.include_router(graph_router,     prefix="/graph")
# LOB and Quantum are separate containers — proxied via httpx
```

### SQL Injection Firewall (`sql_firewall.py`)

```python
import sqlglot

BANNED = ['DROP','TRUNCATE','UNION SELECT','--','/*','xp_','EXEC','information_schema']

async def sql_injection_middleware(request, call_next):
    payload = (await request.body()).decode() + str(request.query_params)
    # String scan
    for pattern in BANNED:
        if pattern.upper() in payload.upper():
            await log_event(ip, 'SQL_INJECTION', payload[:500])
            return JSONResponse({"error": "forbidden"}, status_code=403)
    # AST scan (catches obfuscated payloads)
    try:
        for stmt in sqlglot.parse(payload):
            if type(stmt).__name__ in ('Drop','Truncate','Create','AlterTable'):
                await log_event(ip, 'SQL_INJECTION', payload[:500])
                return JSONResponse({"error": "forbidden"}, status_code=403)
    except Exception:
        pass
    return await call_next(request)
```

### Rate Limiter (`rate_limiter.py`)

```python
# Redis sliding-window — 1,000 req/s/IP
key = f"rl:{client_ip}"
count = await redis_client.incr(key)
if count == 1: await redis_client.expire(key, 1)
if count > 1000:
    await log_event(ip, 'RATE_LIMIT', str(request.url))
    return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
# Fallback if Redis is down: threading.Semaphore token bucket
```

### Prometheus Metrics (`prometheus_metrics.py`)

```python
lob_orders_total      = Counter('lob_orders_total', ..., ['symbol', 'side'])
lob_trades_total      = Counter('lob_trades_total', ..., ['symbol'])
lob_order_latency_ms  = Histogram('lob_order_latency_ms', ...)
lob_active_orders     = Gauge('lob_active_orders', ..., ['symbol'])
sql_injections        = Counter('security_sql_injections_total', ...)
rate_limit_hits       = Counter('security_rate_limit_total', ...)
arb_signals           = Counter('quantum_arbitrage_signals_total', ..., ['method'])
```

### Grafana Dashboard (`hqt_main.json`) — 5 Panels

| Panel | Type | Source | Query / Data |
|-------|------|--------|-------------|
| 1 | Candlestick | TimescaleDB | `SELECT bucket, open, high, low, close FROM ohlcv_1m WHERE symbol=$symbol` |
| 2 | Heatmap | TimescaleDB | LOB depth bids/asks at each price level over time |
| 3 | Bar chart | TimescaleDB | `SELECT bucket, SUM(volume) FROM ohlcv_1m GROUP BY bucket ORDER BY bucket DESC LIMIT 60` |
| 4 | Table | PostgreSQL | `SELECT ts, path, profit_pct, method FROM arbitrage_signals ORDER BY ts DESC LIMIT 20` — both CLASSICAL and QUANTUM rows, colour-coded by method |
| 5 | Time series | Prometheus | `rate(lob_orders_total[1m])` + `histogram_quantile(0.99, rate(lob_order_latency_ms_bucket[1m]))` |

### Siege DDoS Simulation

```bash
siege -c 1000 -t 60S --log=report/siege_ddos_results.txt \
      --content-type "application/json" -f module1_lob/urls.txt
```

Expected: rate limiter blocks excess at 1,001 req/s; `security_events` table gains `RATE_LIMIT` rows; LOB `/health` responds within 2s after siege ends; Grafana Panel 5 shows QPS spike + recovery.
