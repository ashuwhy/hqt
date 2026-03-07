# Module Implementation Specifications
## Hybrid Trading Database System

---

## Module 1 – High-QPS Limit Order Book Engine
**Owner:** Member 1 (Ashutosh Sharma)  
**Language:** Python 3.12 (or Java 21 for performance-critical path)

### Data Structures
| Structure | Purpose | Complexity |
|-----------|---------|-----------|
| `SortedDict` (Red-Black Tree via `sortedcontainers`) | Price-level index | O(log M) insert/delete |
| `collections.deque` per price level | FIFO order queue | O(1) match/cancel |
| `dict[UUID → Order]` | O(1) order lookup by ID for cancel/modify | O(1) |
| Ring buffer (fixed array + head/tail sequence) | LMAX Disruptor inter-thread comms | Lock-free |

### Matching Algorithm
```
PLACE_ORDER(new_order):
  if new_order.side == BUY:
    while ask_tree not empty AND new_order.price >= best_ask AND new_order.qty > 0:
      match against best_ask level FIFO
      emit TRADE event
  else:
    while bid_tree not empty AND new_order.price <= best_bid AND new_order.qty > 0:
      match against best_bid level FIFO
      emit TRADE event
  if new_order.qty > 0:
    insert into corresponding tree at price level
```

### LMAX Ring Buffer
- Fixed array of size `2^20` (1,048,576 slots)
- Three sequence counters: `published_seq`, `consumed_matching_seq`, `consumed_persist_seq`
- No locks: threads spin on sequence numbers
- Inbound thread: increments `published_seq` after writing
- Matching thread: reads up to `published_seq`, processes, advances its sequence
- Persistence thread: reads up to `matching_seq`, batches for DB COPY

### REST + WebSocket API
- Framework: `FastAPI` with `uvicorn`
- LOB engine runs as a separate process; API communicates via internal queue or shared memory
- WebSocket: async generator that publishes depth-diff updates on every trade execution

### Benchmarking
```bash
siege -c 500 -t 30S -f urls.txt           # QPS stress test
python bench_threadpool.py --threads 200 --duration 30  # custom client
```
Target: > 100,000 order ops/sec at p99 < 10ms

---

## Module 2 – TimescaleDB Temporal Analytics Engine
**Owner:** Member 2 (Sujal Anil Kaware)

### Data Ingestion Pipeline
1. Kafka consumer (`confluent-kafka-python`) subscribes to `raw_orders` topic
2. Batches of 1000 records or 100ms timeout → `COPY` to `raw_ticks` via psycopg3
3. Synthetic data generator: `faker` + `numpy` produces 1M+ tick rows for benchmarking

### Hypertable Configuration
- Chunk interval: `1 day` (balances query performance vs. chunk count)
- Space partitioning: 4 partitions by `symbol` hash
- Compression after 7 days; retention after 90 days

### Continuous Aggregates
```
ohlcv_1m   → refresh every 1 minute,  offset 1m
ohlcv_5m   → refresh every 5 minutes, offset 5m
ohlcv_15m  → refresh every 15 minutes, offset 15m
ohlcv_1h   → refresh every 1 hour,    offset 1h
```

### SQL Technical Indicators
All computed as SQL window functions directly in PostgreSQL (no Python pandas):

```sql
-- RSI (14-period) using LAG and window SUM
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
  100 - (100 / (1 + avg_gain/NULLIF(avg_loss, 0))) AS rsi_14
FROM avg_gl;
```

### Performance Benchmark
- Run same OHLCV query on standard PostgreSQL table vs. TimescaleDB hypertable
- Measure query time at 100K, 500K, 1M rows
- Report expected 10–100× speedup for time-range queries

---

## Module 3 – Graph Database Layer (Apache AGE)
**Owner:** Member 3 (Parag Mahadeo Chimankar)

### Graph Schema
- **Nodes:** 20+ `Asset` vertices (10 crypto + 10 fiat currencies)
- **Edges:** `EXCHANGE` directed edges between all active trading pairs
- **Edge properties:** `bid`, `ask`, `spread`, `last_updated`

### Background Edge-Weight Worker
```python
# Runs in asyncio loop, wakes every 500ms
async def update_edge_weights():
    while True:
        rates = await lob_client.get_best_bids()  # internal call
        for (from_sym, to_sym), bid in rates.items():
            await age_conn.execute("""
                SELECT * FROM cypher('fx_graph', $$
                    MATCH (a:Asset {symbol: $from})-[r:EXCHANGE]->(b:Asset {symbol: $to})
                    SET r.bid = $bid, r.last_updated = timestamp()
                $$, $1) AS (result agtype)
            """, json.dumps({"from": from_sym, "to": to_sym, "bid": bid}))
        await asyncio.sleep(0.5)
```

### Classical Arbitrage Baseline (Bellman-Ford)
```python
import math

def bellman_ford_arbitrage(rates_matrix, nodes):
    # Transform: weight = -log(rate)
    # Negative cycle = profitable arbitrage
    dist = {n: float('inf') for n in nodes}
    dist[nodes[0]] = 0
    predecessor = {}
    for _ in range(len(nodes) - 1):
        for (u, v), rate in rates_matrix.items():
            w = -math.log(rate)
            if dist[u] + w < dist[v]:
                dist[v] = dist[u] + w
                predecessor[v] = u
    # Detect negative cycle
    for (u, v), rate in rates_matrix.items():
        if dist[u] + (-math.log(rate)) < dist[v]:
            return extract_cycle(predecessor, v)
    return None
```

### Cypher Queries to Implement
1. Find all profitable 3-hop cycles from USD
2. Find shortest path (most profitable) between any two assets
3. List all edges with spread > threshold
4. Subgraph of crypto-only nodes

---

## Module 4 – Quantum Arbitrage Detection Engine
**Owner:** Member 4 (Kshetrimayum Abo)

### Qiskit Implementation Pipeline

**Step 1: State Encoding**
```python
# For N currency nodes, encode all C(N,3) × 6 directional 3-cycles as basis states
# Use ceil(log2(num_cycles)) qubits
import math
n_qubits = math.ceil(math.log2(num_cycles))
```

**Step 2: Oracle Circuit**
```python
from qiskit import QuantumCircuit, QuantumRegister, ClassicalRegister

def build_oracle(profitable_states: list[int], n_qubits: int) -> QuantumCircuit:
    qr = QuantumRegister(n_qubits, 'q')
    qc = QuantumCircuit(qr)
    for state in profitable_states:
        # Flip ancilla if input matches profitable state (multi-controlled X)
        binary = format(state, f'0{n_qubits}b')
        for i, bit in enumerate(reversed(binary)):
            if bit == '0':
                qc.x(qr[i])
        qc.h(qr[-1])
        qc.mcx(list(range(n_qubits-1)), n_qubits-1)
        qc.h(qr[-1])
        for i, bit in enumerate(reversed(binary)):
            if bit == '0':
                qc.x(qr[i])
    return qc
```

**Step 3: Diffuser (Amplitude Amplification)**
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

**Step 4: Full Grover Circuit**
```python
from qiskit_aer import AerSimulator

def run_grover(exchange_rates, node_symbols):
    cycles = enumerate_3cycles(node_symbols)
    profitable = [i for i, c in enumerate(cycles) if is_profitable(c, exchange_rates)]
    n_qubits = math.ceil(math.log2(len(cycles)))
    n_iterations = int(math.pi / 4 * math.sqrt(len(cycles)))

    qc = QuantumCircuit(n_qubits, n_qubits)
    qc.h(range(n_qubits))  # superposition
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

### Benchmarking Plan
- Graph sizes N ∈ {8, 16, 32, 64}
- For each N: run Qiskit simulation 10× and record `mean_time_ms`
- Run Bellman-Ford 10× for same N, record `mean_time_ms`
- Plot both curves on log-log scale to visualize O(√N) vs O(N)

---

## Module 5 – Security, Observability & DoS Prevention
**Owner:** Member 5 (Kartik Pandey)

### FastAPI Middleware Stack

```python
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import sqlglot, redis.asyncio as aioredis, time

BANNED_PATTERNS = ['DROP','TRUNCATE','UNION SELECT','--','/*','xp_','EXEC','information_schema']

app = FastAPI()
redis_client = aioredis.from_url("redis://localhost:6379")

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    ip = request.client.host
    key = f"rl:{ip}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 1)  # 1-second window
    if count > 1000:  # 1000 req/sec per IP limit
        await log_security_event(ip, "RATE_LIMIT", str(request.url))
        return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
    return await call_next(request)

@app.middleware("http")
async def sql_injection_middleware(request: Request, call_next):
    body = await request.body()
    query_str = body.decode() + str(request.query_params)
    for pattern in BANNED_PATTERNS:
        if pattern.upper() in query_str.upper():
            await log_security_event(request.client.host, "SQL_INJECTION", query_str[:500])
            return JSONResponse({"error": "forbidden query pattern"}, status_code=403)
    try:
        # AST-level check via sqlglot
        if 'sql' in query_str.lower():
            sqlglot.parse(query_str)
    except sqlglot.errors.ParseError:
        pass  # malformed SQL also blocked upstream
    return await call_next(request)
```

### Grafana Dashboard Panels

| Panel # | Type | Data Source | Query |
|---------|------|------------|-------|
| 1 | Candlestick | TimescaleDB | `SELECT bucket, open, high, low, close FROM ohlcv_1m WHERE symbol=$symbol` |
| 2 | Heatmap | TimescaleDB | LOB depth bids/asks at each price level |
| 3 | Bar chart | TimescaleDB | `SELECT bucket, SUM(volume) FROM ohlcv_1m GROUP BY bucket` |
| 4 | Table | PostgreSQL | `SELECT ts, path, profit_pct FROM arbitrage_signals ORDER BY ts DESC LIMIT 20` |
| 5 | Time series | Prometheus | `rate(http_requests_total[1m])` + p99 latency histogram |

### Prometheus Metrics to Expose
```python
from prometheus_client import Counter, Histogram, Gauge

orders_total       = Counter('lob_orders_total', 'Total orders placed', ['symbol','side'])
trades_total       = Counter('lob_trades_total', 'Total matched trades', ['symbol'])
order_latency      = Histogram('lob_order_latency_ms', 'Order processing latency')
active_orders      = Gauge('lob_active_orders', 'Current open orders', ['symbol'])
sql_injections     = Counter('security_sql_injections_total', 'Blocked SQL injection attempts')
rate_limit_hits    = Counter('security_rate_limit_total', 'Rate limit exceeded events')
arbitrage_signals  = Counter('quantum_arbitrage_signals_total', 'Arbitrage cycles detected', ['method'])
```

### Siege DDoS Simulation
```bash
# urls.txt contains LOB PLACE/CANCEL endpoints
siege -c 1000 -t 60S --log=/tmp/siege.log -f urls.txt

# Expected: system stays responsive; rate limiter blocks excess;
# Grafana shows QPS spike and recovery
```
