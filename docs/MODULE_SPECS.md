# Module Implementation Specifications

## Hybrid Trading Database System

---

## Module 1 – High-QPS Limit Order Book Engine

**Owner:** Member 1 (Ashutosh Sharma)
**Language:** C++20 core engine + Python 3.12 FastAPI wrapper

### Core Engine Integration

- **Engine Provider:** External Git Submodule (`https://github.com/ashuwhy/lob`)
- **Mount path:** `module1_lob/engine/`
- **Integration:** Compiled into Python 3.12 via `pybind11` (`olob` module)
- **Wrapper:** FastAPI REST/WebSocket endpoints interacting with the C++ memory space via `olob` bindings
- **Pre-existing benchmarks:** `bench_out/latency_histogram.png` — nanosecond-level; **do not rewrite**

### Setup Command

```bash
git submodule add https://github.com/ashuwhy/lob module1_lob/engine
cd module1_lob/engine && cmake --build build/
pip install -e .   # installs olob Python package
python -c "from olob import OrderBook; print('OK')"
```

### Data Structures (C++ side, exposed via pybind11)

| Structure | Purpose | Complexity |
|-----------|---------|-----------|
| Red-Black Tree (`book_core.cpp`) | Price-level index for bid/ask sides | O(log M) insert/delete |
| Per-price FIFO queue | Price-time priority order matching | O(1) match/cancel |
| `mempool.hpp` arena allocator | Lock-free memory allocation for orders | O(1) alloc |
| LMAX ring buffer (Python `ring_buffer.py`) | Inter-thread communication (inbound → matching → persist) | Lock-free, 2^20 slots |

### Matching Algorithm (from C++ core)

```
PLACE_ORDER(new_order):
  if new_order.side == BUY:
    while ask_tree not empty AND new_order.price >= best_ask AND new_order.qty > 0:
      match against best_ask level FIFO
      emit TradeEvent
  else:
    while bid_tree not empty AND new_order.price <= best_bid AND new_order.qty > 0:
      match against best_bid level FIFO
      emit TradeEvent
  if new_order.qty > 0:
    insert into corresponding tree at price level
```

### LMAX Ring Buffer (Python layer)

- Fixed array of size `2^20` (1,048,576 slots)
- Three sequence counters: `published_seq`, `consumed_matching_seq`, `consumed_persist_seq`
- No locks: threads spin on sequence numbers with memory barriers
- **InboundThread:** Kafka consumer → ring buffer (increments `published_seq`)
- **MatchingThread:** ring buffer → C++ `OrderBook` via `olob` (advances matching seq)
- **PersistenceThread:** matched trades → batch `psycopg3.copy()` to TimescaleDB

### FastAPI Wrapper Endpoints

| Method | Path | Action |
|--------|------|--------|
| `POST` | `/lob/order` | `book.add_limit_order()` or `book.add_market_order()` → DB insert |
| `DELETE` | `/lob/order/{order_id}` | `book.cancel_order()` → status `CANCELLED` |
| `PATCH` | `/lob/order/{order_id}` | Atomic cancel + re-place at new price/qty |
| `GET` | `/lob/depth/{symbol}` | `book.get_depth(levels=10)` → top-10 bids/asks |
| `WS` | `/lob/stream/{symbol}` | Async push `DEPTH_UPDATE` on every match |
| `GET` | `/lob/health` | Active symbols + service status (docker healthcheck) |
| `GET` | `/lob/metrics` | Prometheus metrics exposition |

### Prometheus Metrics (LOB)

```python
lob_orders_total       = Counter('lob_orders_total', ..., ['symbol', 'side'])
lob_trades_total       = Counter('lob_trades_total', ..., ['symbol'])
lob_order_latency_ms   = Histogram('lob_order_latency_ms', ...)
lob_active_orders      = Gauge('lob_active_orders', ..., ['symbol'])
```

### Benchmarking

```bash
siege -c 200 -t 30S -f module1_lob/urls.txt
python module1_lob/bench_threadpool.py --threads 200 --duration 30
```

Target: > 100,000 order ops/sec at p99 < 10ms

---

## Module 2 – TimescaleDB Temporal Analytics Engine

**Language:** Python 3.12

### Data Ingestion Pipeline

1. `kafka_consumer.py` — `confluent_kafka.Consumer` on `raw_orders` topic
2. Batches of 1,000 records or 100ms timeout → `psycopg3.copy()` binary COPY to `raw_ticks`
3. `gen_ticks.py` — `faker` + `numpy` GBM price series; 1M rows, 10 symbols, CLI args: `--rows`, `--symbols`, `--batch-size`

### Hypertable Configuration

- Chunk interval: `1 day` (balances query vs. chunk overhead)
- Space partitioning: 4 partitions by `symbol` hash
- Compression after 7 days (`timescaledb.compress_segmentby = 'symbol'`)
- Retention policy: drop chunks older than 90 days

### Continuous Aggregates

```
ohlcv_1m   → time_bucket('1 minute',  ts)  refresh every 1 minute,  offset 1m
ohlcv_5m   → time_bucket('5 minutes', ts)  refresh every 5 minutes, offset 5m
ohlcv_15m  → time_bucket('15 minutes',ts)  refresh every 15 minutes,offset 15m
ohlcv_1h   → time_bucket('1 hour',    ts)  refresh every 1 hour,    offset 1h
```

All defined in `init.sql` inside idempotent `DO $$ IF NOT EXISTS $$` blocks.

### SQL Technical Indicators (`indicators.sql`)

```sql
-- VWAP
CREATE OR REPLACE FUNCTION fn_vwap(p_symbol TEXT, p_from TIMESTAMPTZ, p_to TIMESTAMPTZ)
RETURNS NUMERIC AS $$
  SELECT SUM(price * volume) / NULLIF(SUM(volume), 0)
  FROM raw_ticks
  WHERE symbol = p_symbol AND ts BETWEEN p_from AND p_to;
$$ LANGUAGE sql STABLE;

-- SMA-20 (from ohlcv_1m)
CREATE OR REPLACE FUNCTION fn_sma20(p_symbol TEXT, p_at TIMESTAMPTZ)
RETURNS NUMERIC AS $$
  SELECT AVG(close)
  FROM (
    SELECT close FROM ohlcv_1m
    WHERE symbol = p_symbol AND bucket <= p_at
    ORDER BY bucket DESC LIMIT 20
  ) t;
$$ LANGUAGE sql STABLE;

-- RSI-14 using LAG + window AVG
WITH gains_losses AS (
  SELECT bucket, symbol, close,
    GREATEST(close - LAG(close) OVER (PARTITION BY symbol ORDER BY bucket), 0) AS gain,
    GREATEST(LAG(close) OVER (PARTITION BY symbol ORDER BY bucket) - close, 0) AS loss
  FROM ohlcv_1m
),
avg_gl AS (
  SELECT bucket, symbol, close,
    AVG(gain) OVER (PARTITION BY symbol ORDER BY bucket ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_gain,
    AVG(loss) OVER (PARTITION BY symbol ORDER BY bucket ROWS BETWEEN 13 PRECEDING AND CURRENT ROW) AS avg_loss
  FROM gains_losses
)
SELECT bucket, symbol, close,
  100 - (100 / (1 + avg_gain / NULLIF(avg_loss, 0))) AS rsi_14
FROM avg_gl;
```

### Analytics API (`analytics_api.py`)

```
GET /analytics/ticks?symbol=&from=&to=&limit=
GET /analytics/ohlcv?symbol=&interval=1m|5m|15m|1h&from=&to=&limit=
GET /analytics/indicators?symbol=&indicator=vwap|sma20|bollinger|rsi&from=&to=
```

Mounted into `module5_security/main.py` under `/analytics`.

### Performance Benchmark (`bench_timescale.py`)

- Create plain table + hypertable, load same 1M rows each
- Run same OHLCV query 10× on each, record times
- Write results to `benchmark_runs` table
- Expected result: hypertable ≥ 10× faster on time-range queries at 1M rows

---

## Module 3 – Graph Database Layer (Apache AGE)

**Language:** Python 3.12 + Cypher (via AGE)

### Graph Schema

- **Nodes:** 20+ `Asset` vertices (10 crypto + 10 fiat currencies)
- **Edges:** Directed `EXCHANGE` edges for all active trading pairs
- **Edge properties:** `bid FLOAT`, `ask FLOAT`, `spread FLOAT`, `last_updated BIGINT`

### Initialization (`graph_init.py`)

```python
# Idempotent — uses MERGE to avoid duplicates
CYPHER_CREATE_ASSET = """
    SELECT * FROM cypher('fx_graph', $$
        MERGE (:Asset {symbol: %s, name: %s, asset_class: %s})
    $$) AS (v agtype)
"""
```

Assets: BTC, ETH, BNB, SOL, ADA, XRP, DOGE, AVAX, MATIC, DOT (crypto)
       + USD, EUR, GBP, JPY, AUD, CAD, CHF, INR, SGD, HKD (fiat)

### Background Edge-Weight Worker (`edge_weight_updater.py`)

```python
async def update_edge_weights():
    while True:
        try:
            for symbol in ACTIVE_PAIRS:
                depth = await lob_client.get(f"/lob/depth/{symbol}")
                best_bid = depth["bids"][0][0]
                await age_conn.execute("""
                    SELECT * FROM cypher('fx_graph', $$
                        MATCH (a:Asset {symbol: $from})-[r:EXCHANGE]->(b:Asset {symbol: $to})
                        SET r.bid = $bid, r.last_updated = timestamp()
                    $$, $1) AS (result agtype)
                """, json.dumps({"from": from_sym, "to": to_sym, "bid": float(best_bid)}))
        except Exception as e:
            logger.warning(f"LOB unavailable: {e} — skipping update cycle")
        await asyncio.sleep(0.5)
```

### Classical Arbitrage Baseline (`bellman_ford.py`)

```python
import math

def bellman_ford_arbitrage(rates_matrix: dict, nodes: list) -> list | None:
    """Transform: weight = -log(rate). Negative cycle = profitable arbitrage."""
    dist = {n: float('inf') for n in nodes}
    dist[nodes[0]] = 0
    predecessor = {}
    for _ in range(len(nodes) - 1):
        for (u, v), rate in rates_matrix.items():
            w = -math.log(rate)
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                predecessor[v] = u
    for (u, v), rate in rates_matrix.items():
        if dist[u] + (-math.log(rate)) < dist[v]:
            return extract_cycle(predecessor, v)
    return None
```

### Cypher Queries to Implement (`graph_queries.py`)

1. `find_3hop_arbitrage_cycles(from_symbol)` — all profitable 3-hop directed cycles
2. `find_shortest_path(from_sym, to_sym)` — most profitable exchange path
3. `find_high_spread_edges(threshold)` — edges with spread > threshold
4. `crypto_subgraph()` — subgraph of crypto-only Asset nodes

### Graph API (`graph_api.py`)

```
GET /graph/nodes              → list all Asset vertices
GET /graph/edges              → list EXCHANGE edges with current bid/ask
GET /graph/paths?from_symbol= → 3-hop cycle search from given node
```

---

## Module 4 – Quantum Arbitrage Detection Engine

**Language:** Python 3.12 + Qiskit 0.45 + qiskit-aer 0.13

### Qiskit Pipeline

**Step 1 — State Encoding**

```python
# Encode all P(N,3) = N!/(N-3)! directed 3-cycles as basis states
n_qubits = math.ceil(math.log2(len(all_cycles)))
# N=8  → 336 cycles → 9 qubits
# N=16 → 3360 cycles → 12 qubits
# N=32 → 29760 cycles → 15 qubits
```

**Step 2 — Oracle (`grover_oracle.py`)**

```python
def build_oracle(profitable_states: list[int], n_qubits: int) -> QuantumCircuit:
    qr = QuantumRegister(n_qubits, 'q')
    qc = QuantumCircuit(qr)
    for state in profitable_states:
        binary = format(state, f'0{n_qubits}b')
        for i, bit in enumerate(reversed(binary)):
            if bit == '0':
                qc.x(qr[i])
        qc.h(qr[-1])
        qc.mcx(list(range(n_qubits - 1)), n_qubits - 1)
        qc.h(qr[-1])
        for i, bit in enumerate(reversed(binary)):
            if bit == '0':
                qc.x(qr[i])
    return qc
```

**Step 3 — Diffuser (`grover_diffuser.py`)**

```python
def build_diffuser(n_qubits: int) -> QuantumCircuit:
    qc = QuantumCircuit(n_qubits)
    qc.h(range(n_qubits))
    qc.x(range(n_qubits))
    qc.h(n_qubits - 1)
    qc.mcx(list(range(n_qubits - 1)), n_qubits - 1)
    qc.h(n_qubits - 1)
    qc.x(range(n_qubits))
    qc.h(range(n_qubits))
    return qc
```

**Step 4 — Full Grover Circuit (`run_grover.py`)**

```python
from qiskit_aer import AerSimulator

def run_grover(exchange_rates: dict, node_symbols: list) -> list | None:
    cycles = enumerate_3cycles(node_symbols)
    profitable = [i for i, c in enumerate(cycles) if is_profitable(c, exchange_rates)]
    n_qubits = math.ceil(math.log2(len(cycles)))
    n_iterations = max(1, int(math.pi / 4 * math.sqrt(len(cycles) / max(len(profitable), 1))))

    qc = QuantumCircuit(n_qubits, n_qubits)
    qc.h(range(n_qubits))
    for _ in range(n_iterations):
        qc.compose(build_oracle(profitable, n_qubits), inplace=True)
        qc.compose(build_diffuser(n_qubits), inplace=True)
    qc.measure(range(n_qubits), range(n_qubits))

    simulator = AerSimulator()
    result = simulator.run(qc, shots=1024).result()
    counts = result.get_counts()
    best_state = int(max(counts, key=counts.get), 2)
    return cycles[best_state] if best_state < len(cycles) else None
```

### Benchmarking Plan (`benchmark_quantum.py`)

- Graph sizes N ∈ {8, 16, 32, 64}
- For each N: run Grover simulation 10× → record `mean_ms ± std_ms`
- Run Bellman-Ford 10× for same N → record `mean_ms ± std_ms`
- Output: `benchmark_quantum.csv` (committed to repo)
- Plot: `quantum_scaling.png` — log-log chart with O(√N) and O(N) reference lines

### Quantum API (`quantum_api.py`)

```
POST /quantum/run-grover   { "graph_size_n": 16, "method": "QUANTUM" }
GET  /quantum/signals      ?limit=50&method=QUANTUM|CLASSICAL|ALL
```

### DB Schema — `arbitrage_signals`

```sql
-- signal_id UUID PRIMARY KEY (single-column — fixed from init.sql)
-- path TEXT[] e.g. ARRAY['USD','BTC','ETH','USD']
-- profit_pct NUMERIC(10,6)
-- method VARCHAR(20) CHECK (method IN ('QUANTUM','CLASSICAL'))
-- circuit_depth INT, grover_iterations INT
-- classical_ms NUMERIC(10,3), quantum_ms NUMERIC(10,3)
-- graph_size_n INT NOT NULL
```

---

## Module 5 – Security, Observability & DoS Prevention

**Language:** Python 3.12 + FastAPI + Redis + Prometheus

### Application Entry Point (`main.py`)

```python
from fastapi import FastAPI
from module1_lob.lob_api import router as lob_router
from module2_timescale.analytics_api import router as analytics_router
from module3_graph.graph_api import router as graph_router
from module4_quantum.quantum_api import router as quantum_router

app = FastAPI(title="HQT Security Proxy")
app.include_router(lob_router,       prefix="/lob")
app.include_router(analytics_router, prefix="/analytics")
app.include_router(graph_router,     prefix="/graph")
app.include_router(quantum_router,   prefix="/quantum")
```

### SQL Injection Firewall (`sql_firewall.py`)

- Uses `sqlglot` for AST-level parsing — detects `DROP`, `TRUNCATE`, `UNION SELECT`, multi-statement injection
- Banned pattern scan (string match): `--`, `/*`, `xp_`, `EXEC`, `information_schema`
- On detection: HTTP 403 + INSERT into `security_events (SQL_INJECTION)`

### Rate Limiter (`rate_limiter.py`)

```python
# Redis sliding-window — 1000 req/sec per IP
key = f"rl:{client_ip}"
count = await redis_client.incr(key)
if count == 1: await redis_client.expire(key, 1)
if count > 1000:
    await log_event(ip, "RATE_LIMIT", endpoint)
    return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
# Fallback: in-process token bucket if Redis is down
```

### Prometheus Metrics (`prometheus_metrics.py`)

```python
orders_total       = Counter('lob_orders_total',           ..., ['symbol', 'side'])
trades_total       = Counter('lob_trades_total',           ..., ['symbol'])
order_latency      = Histogram('lob_order_latency_ms',     ...)
active_orders      = Gauge('lob_active_orders',            ..., ['symbol'])
sql_injections     = Counter('security_sql_injections_total', ...)
rate_limit_hits    = Counter('security_rate_limit_total',  ...)
arb_signals        = Counter('quantum_arbitrage_signals_total', ..., ['method'])
```

### Grafana Dashboard (`grafana_provisioning/dashboards/hqt_main.json`)

| Panel | Type | Data Source | Query |
|-------|------|------------|-------|
| 1 | Candlestick | TimescaleDB | `SELECT bucket, open, high, low, close FROM ohlcv_1m WHERE symbol=$symbol` |
| 2 | Heatmap | TimescaleDB | LOB depth bids/asks at each price level over time |
| 3 | Bar chart | TimescaleDB | `SELECT bucket, SUM(volume) FROM ohlcv_1m GROUP BY bucket` |
| 4 | Table | PostgreSQL | `SELECT ts, path, profit_pct, method FROM arbitrage_signals ORDER BY ts DESC LIMIT 20` |
| 5 | Time series | Prometheus | `rate(http_requests_total[1m])` + p99 latency histogram |

### Siege DDoS Simulation

```bash
siege -c 1000 -t 60S --log=report/siege_ddos_results.txt -f module1_lob/urls.txt
```

Expected: rate limiter blocks excess; `security_events` gains `RATE_LIMIT` rows;
LOB `/health` responds within 2s after siege stops; Grafana shows QPS spike + recovery.
