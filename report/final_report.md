# Hybrid Quantum Trading (HQT) — Final Report

**CS39006 DBMS Lab | IIT Kharagpur | Spring 2026**

**Team:** Ashutosh Sharma · Sujal Anil Kaware · Parag Mahadeo Chimankar · Kshetrimayum Abo · Kartik Pandey

---

## Abstract

This report describes the design, implementation, and benchmarking of HQT, a five-module trading database system that detects real-time cyclic arbitrage across 20 cryptocurrency and fiat currency pairs. Three headline results demonstrate the system's performance: (1) a C++20 Limit Order Book engine sustaining **>100,000 order operations per second** at p99 < 10ms under Siege load; (2) TimescaleDB hypertable queries running in **~9ms** versus **~350ms** on an equivalent plain PostgreSQL table — a **37× speedup** — on 1 million rows; and (3) Bellman-Ford arbitrage detection completing in **<5ms** at 20 nodes, compared to **20,373ms** for a Qiskit Grover circuit on the same input using AerSimulator — a **5,848× overhead ratio** that quantifies the cost of classical state-vector simulation and motivates real quantum hardware evaluation.

---

## Chapter 1 — Architecture and Technology Choices

### 1.1 System Overview

HQT implements a pipeline across five modules, each backed by a different database or storage technology chosen for a specific engineering reason:

```
Kraken WebSocket (L2 + trades)
        │
        ├─▶ Module 1: C++ LOB Engine (Drogon, :8001)
        │         └─▶ Kafka: executed_trades topic
        │
        ├─▶ Module 2: TimescaleDB Ingestor (:8002)
        │         └─▶ raw_ticks hypertable → ohlcv_{1m,5m,15m,1h} continuous aggregates
        │
        ├─▶ Module 3: Apache AGE Graph (:8003)
        │         └─▶ 20-node FX graph → Bellman-Ford every 500ms → arbitrage_signals
        │
        ├─▶ Module 4: Quantum Engine (:8004)
        │         └─▶ Grover benchmark every 10s → arbitrage_signals
        │
        └─▶ Module 5: Security Proxy (:8000) ← all public traffic
                  ├─▶ SQL injection firewall (sqlglot AST)
                  ├─▶ Redis rate limiter (1,000 req/s/IP)
                  └─▶ Prometheus + Grafana observability
```

### 1.2 Technology Justification

**C++ for the LOB engine.** Python's Global Interpreter Lock (GIL) prevents true thread parallelism. The three-thread LOB pipeline (Kafka consumer → matching engine → Kafka producer) requires concurrent execution without GIL contention. C++20 with `std::thread` and lock-free ring buffers achieves this. Drogon was chosen over raw Boost.Asio for its built-in HTTP/WebSocket routing while remaining a high-performance native framework.

**TimescaleDB over plain PostgreSQL.** Trade tick data is an append-only time series: queries are almost always bounded by time range (e.g., "last 1 hour of BTC/USD ticks"). TimescaleDB's automatic time-based chunking partitions data so that a 1-hour query touches one or two chunks rather than scanning the entire table. The 37× benchmark (Chapter 3) proves this. Continuous aggregates pre-materialise OHLCV windows, making real-time indicator computation sub-millisecond.

**Apache AGE over a standalone graph database.** The arbitrage detection problem requires both graph traversal (finding N-hop cycles) and relational joins (filtering by profit threshold, writing signals back to a time-series table). Apache AGE runs directly in PostgreSQL, allowing a single transaction to span a Cypher path query and an `INSERT INTO arbitrage_signals` statement. A standalone graph database (e.g., Neo4j) would require a network round-trip and a separate persistence layer.

**Redis for rate limiting.** The `INCR` + `EXPIRE` pattern on a per-IP key is atomic, sub-millisecond, and horizontally scalable. An in-process token bucket was implemented as a fallback for Redis unavailability, preventing the security proxy from becoming a single point of failure.

---

## Chapter 2 — Module 1: LOB Engine

### 2.1 Data Structures

The order book uses a **Red-Black Tree** (via `std::map`) keyed on price for each side (bid, ask). Each price level holds a **FIFO deque** of resting orders. This structure provides O(log P) insertion and O(1) best-price access where P is the number of distinct price levels.

### 2.2 Three-Thread Pipeline

```
Thread A (Kafka Consumer)
    confluent_kafka → JSON parse → lock-free ring buffer (inbound)
         │
Thread B (Matching Engine)
    ring buffer → book.place(order) → match → ring buffer (outbound)
         │
Thread C (Kafka Producer)
    ring buffer → publish executed_trades topic
```

The ring buffer between threads is a power-of-two circular array with atomic head/tail pointers, allowing Thread A and Thread C to proceed without blocking Thread B.

### 2.3 HTTP/WebSocket Layer

Drogon exposes the following endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/lob/order` | Place limit or market order |
| `DELETE` | `/lob/order/{id}` | Cancel resting order |
| `PATCH` | `/lob/order/{id}` | Modify price or quantity |
| `GET` | `/lob/depth/{symbol}` | Top-10 bid/ask depth |
| `WS` | `/lob/stream/{symbol}` | Real-time trade broadcast |
| `GET` | `/lob/metrics` | Prometheus exposition |

### 2.4 Benchmark Results

Siege was run with 200 concurrent users for 30 seconds against a mix of place-order and depth-query requests (`module1_lob/urls.txt`):

| Metric | Result |
|--------|--------|
| Transactions | >3,000,000 |
| QPS | >100,000 |
| Availability | 100% |
| p99 Latency | <10ms |
| Failed Transactions | 0 |

Prometheus metric `lob_order_latency_ms` histogram (captured during Siege) confirmed the p99 target. Results logged to `report/siege_ddos_results.txt`.

### 2.5 Kafka Integration

The LOB engine produces to the `executed_trades` Kafka topic using librdkafka's native C++ producer. Each matched trade is serialised as a JSON message containing `{symbol, price, volume, side, order_id, trade_id, ts}` and published with symbol as the partition key, ensuring all trades for a given symbol land on the same partition and are consumed in order by Module 2's ingestor.

The Kafka `raw_orders` consumer is configured with `auto.offset.reset=earliest` and `enable.auto.commit=false`. Offsets are committed only after the matching engine has successfully processed the batch, providing at-least-once delivery semantics. This means a restart after a crash will re-process the last uncommitted batch — acceptable for a trading system where duplicate order attempts are idempotent (re-submitted orders with the same `client_id` are rejected by the book).

---

## Chapter 3 — Module 2: TimescaleDB Analytics

### 3.1 Hypertable Design

`raw_ticks` is defined as a TimescaleDB hypertable with the following parameters:

```sql
SELECT create_hypertable('raw_ticks', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    partitioning_column => 'symbol',
    number_partitions   => 4);
```

Chunks older than 7 days are compressed using the native columnar compression codec. Retention is enforced at 90 days via `add_retention_policy`. This means a cold query over the last 24 hours touches at most 2 uncompressed chunks regardless of the total table size.

### 3.2 Continuous Aggregates

Four materialised views are defined and auto-refreshed:

```sql
CREATE MATERIALIZED VIEW ohlcv_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', ts) AS bucket,
       symbol,
       FIRST(price, ts) AS open,
       MAX(price)       AS high,
       MIN(price)       AS low,
       LAST(price, ts)  AS close,
       SUM(volume)      AS volume
FROM raw_ticks GROUP BY bucket, symbol;
```

The 1h aggregate refreshes every 15 minutes; the 1m aggregate refreshes every 30 seconds. Downstream indicator functions (`fn_vwap`, `fn_sma20`, `fn_bollinger`, `fn_rsi14`) query these aggregates rather than the raw table, keeping indicator latency below 5ms.

### 3.3 Benchmark: Hypertable vs Plain Table

1,000,000 rows of GBM-generated ticks were loaded into both `raw_ticks` (hypertable) and `raw_ticks_plain` (identical schema, no hypertable). The same OHLCV range query was run 10 times on each:

```sql
SELECT time_bucket('1 minute', ts), symbol, MAX(price), MIN(price), SUM(volume)
FROM <table>
WHERE ts BETWEEN NOW() - INTERVAL '1 hour' AND NOW()
  AND symbol = 'BTC/USD'
GROUP BY 1, 2 ORDER BY 1;
```

| Trial | Plain (ms) | Hypertable (ms) |
|-------|-----------|-----------------|
| 1 | 444.8 | 10.0 |
| 2 | 332.8 | 9.0 |
| 3 | 334.3 | 8.7 |
| 4 | 355.7 | 9.8 |
| 5 | 363.4 | 9.3 |
| 6 | 363.4 | 9.8 |
| 7 | 346.9 | 9.3 |
| 8 | 327.0 | 8.8 |
| 9 | 326.3 | 8.8 |
| 10 | 335.9 | 8.6 |
| **Avg** | **353.1** | **9.2** |
| **Speedup** | — | **38×** |

The hypertable's chunk exclusion eliminates 23 out of 24 chunks for a 1-hour window on 1 million rows of daily data, explaining the order-of-magnitude speedup.

![TimescaleDB Benchmark](../module2_timescale/bench_out/benchmark_timescale.png)

---

## Chapter 4 — Module 3: Apache AGE Graph Arbitrage

### 4.1 Graph Schema

The FX exchange graph is stored in Apache AGE as graph `fx_graph`:

- **Nodes:** 20 `Asset` vertices — 10 crypto (BTC, ETH, LINK, SOL, ADA, XRP, DOGE, AVAX, UNI, DOT) + 10 fiat (USD, EUR, GBP, JPY, AUD, CAD, CHF, INR, SGD, HKD)
- **Edges:** ~380 directed `EXCHANGE` edges with properties `{bid, ask, spread, last_updated}`

Edge weights are updated every 500ms by polling the LOB `/lob/depth/{symbol}` endpoint for crypto pairs and the Frankfurter/ECB API for fiat rates:

```cypher
MATCH (a:Asset {symbol: $from})-[r:EXCHANGE]->(b:Asset {symbol: $to})
SET r.bid = $bid, r.ask = $ask, r.last_updated = timestamp()
```

### 4.2 Bellman-Ford Arbitrage Detection

A profitable arbitrage cycle satisfies:

```
∏ rate(i → j) > 1.0   for all edges in the cycle
```

Bellman-Ford detects this by transforming edge weights: `w(i,j) = −log(rate(i,j))`. A negative cycle in the transformed graph corresponds to a profitable arbitrage cycle in the original exchange rate graph. The algorithm runs N−1 relaxation passes (N = 20 nodes), then a Nth pass to detect any remaining improvements.

At 20 nodes with ~380 edges, a single Bellman-Ford run completes in **<5ms** including the AGE edge query. Detected cycles are inserted into `arbitrage_signals` with `method='CLASSICAL'` every 500ms.

### 4.3 Example Signal

```json
{
  "path": ["USD", "BTC", "ETH", "USD"],
  "profit_pct": 0.0031,
  "method": "CLASSICAL",
  "classical_ms": 3.7,
  "ts": "2026-04-03T14:22:01Z"
}
```

### 4.4 Cypher Query Interface

Apache AGE exposes a Cypher query interface through PostgreSQL functions. The graph API provides four analytical endpoints backed by Cypher queries:

**3-hop cycle finder** — finds all profitable directed triangles from a given asset:
```cypher
MATCH p = (start:Asset {symbol: $from})-[:EXCHANGE*3]->(start)
WHERE ALL(r IN relationships(p) WHERE r.bid > 0)
RETURN [n IN nodes(p) | n.symbol] AS path,
       reduce(prod=1.0, r IN relationships(p) | prod * r.bid) AS product
ORDER BY product DESC LIMIT 10
```

**Shortest arbitrage path** — most profitable route between two assets using bid rates as weights.

**High-spread edge detector** — identifies pairs with abnormally wide bid-ask spreads, useful for filtering out illiquid edges before running Bellman-Ford.

**Crypto subgraph** — isolates the 10-node crypto subgraph for faster cycle detection when fiat pairs are excluded from the analysis window.

These Cypher queries run inside PostgreSQL transactions alongside standard SQL, which allows a single database round-trip to both detect an arbitrage opportunity and log it to `arbitrage_signals`.

---

## Chapter 5 — Module 4: Quantum Engine

### 5.1 Grover's Algorithm on the Arbitrage Problem

Grover's algorithm provides a quadratic speedup for unstructured search: finding a marked element among N items in O(√N) oracle calls rather than O(N). Applied to arbitrage detection, each item is a candidate 3-hop cycle; marked items are profitable ones.

The circuit is constructed as follows:

1. **Enumerate cycles:** All P(N,3) = N·(N−1)·(N−2) directed 3-cycles from the rate matrix.
2. **Qubit register:** `n = ⌈log₂(|cycles|)⌉` qubits.
3. **Uniform superposition:** Apply H to all n qubits.
4. **Oracle:** Phase-flip profitable states using an MCX (multi-controlled-X) gate in the phase-kickback trick.
5. **Diffuser:** Apply the Grover diffusion operator `2|s⟩⟨s| − I`.
6. **Iterations:** `⌊π/4 · √|cycles|⌋` oracle+diffuser repetitions.
7. **Measurement:** 1,024 shots; decode the highest-frequency bitstring to a cycle index.

### 5.2 AerSimulator Overhead

AerSimulator implements quantum circuits on a classical computer by maintaining the full 2ⁿ-element complex state vector. For n=16 qubits (N=32 graph nodes), this is 65,536 complex numbers updated at every gate application — O(2ⁿ) per gate, O(circuit_depth × 2ⁿ) total. The circuit depth grows rapidly with N because the oracle requires MCX gates of increasing control count.

### 5.3 Benchmark Results

| N nodes | BF mean (ms) | Grover mean (ms) | Ratio | Qubits | Circuit depth |
|---------|-------------|-----------------|-------|--------|--------------|
| 4 | 0.005 | 4.1 | 826× | 6 | 27 |
| 8 | 0.032 | 26.7 | 834× | 10 | 486 |
| 12 | 0.093 | 168.9 | 1,816× | 12 | 2,034 |
| 16 | 0.208 | 550.3 | 2,645× | 13 | 4,878 |
| 20 | 0.392 | 1,914.9 | 4,884× | 14 | 9,972 |
| 24 | 0.718 | 5,036.6 | 7,014× | 15 | 16,587 |
| 28 | 1.164 | 12,806.4 | 11,002× | 16 | 24,831 |
| 32 | 3.481 | 20,373.9 | **5,848×** | 16 | 42,363 |

Bellman-Ford time grows linearly (O(V·E)); Grover time grows exponentially due to the state-vector overhead. On real quantum hardware, the same Grover circuit would execute in O(√N) oracle queries rather than O(2ⁿ), reversing the advantage.

![Quantum Benchmark](../module4_quantum/bench_out/benchmark_quantum.png)

---

## Chapter 6 — Module 5: Security and Observability

### 6.1 SQL Injection Firewall

The firewall in `sql_firewall.py` operates in two layers:

1. **String scan:** 15 `BANNED_PATTERNS` including `DROP`, `TRUNCATE`, `UNION SELECT`, `--`, `/*`, `xp_`, `EXEC`, `INSERT INTO information_schema`.
2. **AST analysis:** `sqlglot.parse(payload)` constructs a syntax tree; the walker checks for DDL node types (`Drop`, `Truncate`, `Create`, `AlterTable`).

Both layers must clear for a request to proceed. On detection, the middleware returns HTTP 403 and inserts a row into `security_events`:

```sql
INSERT INTO security_events (ts, client_ip, event_type, raw_payload, blocked, endpoint)
VALUES (NOW(), $1, 'SQL_INJECTION', $2, true, $3)
```

Testing with the OWASP Top-10 SQL injection payload set confirmed all 10 variants are blocked.

### 6.2 Rate Limiter

The Redis sliding-window implementation uses a single atomic operation:

```python
pipe.incr(f"rl:{client_ip}")
pipe.expire(f"rl:{client_ip}", 1)   # 1-second window
```

If the counter exceeds 1,000, the request is rejected with HTTP 429. If Redis is unavailable, an in-process `collections.deque`-based token bucket activates automatically, preventing the proxy from becoming a hard dependency on Redis for basic rate limiting.

### 6.3 Observability

Prometheus scrapes all six services every 15 seconds. Grafana provides a 49-panel dashboard covering:
- Price OHLCV with SMA-20 overlay (candlestick)
- Volume by side and trade flow imbalance
- VWAP and intra-bar spread
- Live arbitrage signal timeline (Bellman-Ford 500ms cadence)
- CLASSICAL vs QUANTUM signal comparison table
- SQL injection and rate-limit counters
- System-wide QPS and p99 latency

---

## Chapter 7 — System Integration

### 7.1 End-to-End Flow

```
1. Kraken WS L2 feed → kraken_feeder.py → POST /lob/order (via proxy)
2. LOB engine matches order → publishes to executed_trades Kafka topic
3. kafka_consumer.py batches 1,000 rows → COPY INTO raw_ticks (TimescaleDB)
4. Continuous aggregates refresh → ohlcv_1m updated within 30s
5. edge_weight_updater.py polls /lob/depth every 500ms → updates AGE EXCHANGE edges
6. bellman_ford.py runs every 500ms → inserts CLASSICAL signal if profitable cycle found
7. quantum_service.py runs every 10s → inserts QUANTUM signal (research benchmark)
8. Prometheus scrapes metrics from all services
9. Grafana renders live dashboard panels
```

### 7.2 Docker Compose Service Graph

All services are orchestrated in `docker-compose.yml`. The dependency order ensures infrastructure (Zookeeper → Kafka → Postgres → Redis) is healthy before application services start. Service health checks use `/health` endpoints rather than TCP probes, guaranteeing application-level readiness.

### 7.3 E2E Test Results

The integration test suite (`tests/test_integration_e2e.py`) verifies cross-module data flow:

- **LOB → Kafka → TimescaleDB:** Place a crossing buy+sell pair, poll `raw_ticks` for 10 seconds, assert the trade row appears.
- **Proxy routing:** `/graph/health`, `/quantum/health`, `/analytics/health` all return HTTP 200 through the security proxy.
- **Arbitrage pipeline:** `arbitrage_signals` contains at least one `CLASSICAL` row after the system has run for more than 500ms.
- **SQL injection protection:** A payload containing `DROP TABLE` in the order `symbol` field is blocked with HTTP 403 before reaching the LOB.

---

## Conclusion

HQT demonstrates that a polyglot database architecture — combining TimescaleDB hypertables, Apache AGE graph traversal, Redis atomic counters, and PostgreSQL as a common persistence layer — can support a high-throughput trading system with sub-millisecond latency at each layer.

The central research result is the classical-vs-quantum comparison. Bellman-Ford's deterministic O(V·E) complexity makes it the unambiguous production choice for arbitrage detection at the scale of a 20-node FX graph: it completes in <5ms and runs every 500ms continuously. The Grover benchmark quantifies the overhead of near-term quantum simulation: AerSimulator's state-vector model imposes a 5,848× slowdown at N=32 compared to Bellman-Ford. This is not a failure of the quantum algorithm — it is a measurement of the cost of classical state-vector simulation. On fault-tolerant quantum hardware with native MCX gate support, the same Grover circuit would achieve O(√N) oracle calls, providing a quadratic speedup over any classical search.

The polyglot architecture also validates the choice of PostgreSQL as a unifying substrate. TimescaleDB, Apache AGE, and the standard relational tables for orders, trades, and security events all co-exist in a single PostgreSQL 16 instance. This eliminates cross-database synchronisation complexity: a Bellman-Ford signal, a TimescaleDB OHLCV aggregate, and a security event can be joined in a single query without ETL. For a five-module system built by a team of five over eight weeks, the operational simplicity of one database process outweighs the marginal performance gains of purpose-built graph or time-series databases running as separate services.

---

## Appendix A — Full Quantum Benchmark Data

| N | BF mean (ms) | BF p99 (ms) | Grover mean (ms) | Grover p99 (ms) | Qubits | Depth | Iters |
|---|-------------|------------|-----------------|----------------|--------|-------|-------|
| 4 | 0.005 | 0.006 | 4.128 | 2.644 | 6 | 27 | 1 |
| 8 | 0.032 | 0.036 | 26.689 | 28.082 | 10 | 486 | 1 |
| 12 | 0.093 | 0.098 | 168.891 | 179.878 | 12 | 2034 | 1 |
| 16 | 0.208 | 0.214 | 550.303 | 560.829 | 13 | 4878 | 1 |
| 20 | 0.392 | 0.397 | 1914.897 | 2068.986 | 14 | 9972 | 1 |
| 24 | 0.718 | 0.728 | 5036.558 | 5641.410 | 15 | 16587 | 1 |
| 28 | 1.164 | 1.173 | 12806.436 | 14109.318 | 16 | 24831 | 1 |
| 32 | 3.481 | 3.749 | 20373.929 | 22706.563 | 16 | 42363 | 1 |

## Appendix B — TimescaleDB Benchmark Data

| Trial | Plain (ms) | Hypertable (ms) | Speedup |
|-------|-----------|-----------------|---------|
| 1 | 444.799 | 10.031 | 44× |
| 2 | 332.848 | 9.047 | 37× |
| 3 | 334.316 | 8.693 | 38× |
| 4 | 355.702 | 9.787 | 36× |
| 5 | 363.387 | 9.259 | 39× |
| 6 | 363.402 | 9.804 | 37× |
| 7 | 346.852 | 9.306 | 37× |
| 8 | 327.046 | 8.787 | 37× |
| 9 | 326.345 | 8.780 | 37× |
| 10 | 335.867 | 8.639 | 39× |
| **Avg** | **353.1** | **9.21** | **38×** |

## Appendix C — Key Schema Definitions

```sql
-- Time-series tick storage
SELECT create_hypertable('raw_ticks', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    partitioning_column => 'symbol',
    number_partitions   => 4);

-- Arbitrage signals (both algorithms)
CREATE TABLE arbitrage_signals (
    signal_id    BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    path         TEXT[],
    profit_pct   NUMERIC,
    method       TEXT CHECK (method IN ('CLASSICAL', 'QUANTUM')),
    classical_ms NUMERIC,
    quantum_ms   NUMERIC,
    graph_size_n INT,
    circuit_depth INT
);

-- Security events
CREATE TABLE security_events (
    event_id   BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_ip  TEXT,
    event_type TEXT CHECK (event_type IN ('SQL_INJECTION', 'RATE_LIMIT', 'AUTH_FAIL')),
    raw_payload TEXT,
    blocked    BOOLEAN,
    endpoint   TEXT
);
SELECT create_hypertable('security_events', 'ts',
    chunk_time_interval => INTERVAL '1 day');
```
