# Hybrid Quantum Trading (HQT) — Final Project Report

**CS39006 Database Management Systems Lab | IIT Kharagpur | Spring 2026**

---

## 1. Title and Team Members

**Project Title:** HQT — Hybrid Quantum Trading: A Polyglot Database System for Real-Time Arbitrage Detection

| Name | Roll Number | Module |
|------|-------------|--------|
| Ashutosh Sharma | — | Module 1: C++ LOB Engine |
| Sujal Anil Kaware | — | Module 2: TimescaleDB Analytics |
| Parag Mahadeo Chimankar | — | Module 3: Apache AGE Graph Arbitrage |
| Kshetrimayum Abo | — | Module 4: Quantum Engine |
| Kartik Pandey | — | Module 5: Security & Observability |

---

## 2. Objective

HQT is a five-module trading database system that detects real-time cyclic arbitrage opportunities across 20 cryptocurrency and fiat currency pairs. The system is designed to answer the question: *which database technologies best serve the distinct data access patterns of a high-frequency trading pipeline?*

Concretely, HQT pursues three measurable goals:

1. **Throughput:** sustain >100,000 order operations/second at p99 latency <10ms using a C++20 Limit Order Book engine backed by Kafka.
2. **Time-series analytics:** demonstrate that TimescaleDB hypertables are measurably faster than plain PostgreSQL for financial time-series queries — targeting a ≥10× speedup on 1 million rows.
3. **Arbitrage detection:** run Bellman-Ford on a live Apache AGE graph of FX exchange rates every 500ms and compare its wall-clock time against a Qiskit Grover's Algorithm benchmark on the same input.

A secondary research objective is to quantify the gap between near-term quantum simulation (AerSimulator) and theoretical quantum advantage — producing a benchmark chart that shows O(V·E) Bellman-Ford versus exponentially-growing Grover simulator overhead, and explaining what real quantum hardware would change.

---

## 3. Methodology

### 3.1 System Architecture

HQT is a microservices system where each module is backed by a different database technology chosen for specific access-pattern requirements. All public traffic routes through a FastAPI security proxy on port 8000.

```
Kraken WebSocket (L2 order book + trades)
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
        └─▶ Module 5: Security Proxy (:8000)
                  ├─▶ SQL injection firewall (sqlglot AST)
                  ├─▶ Redis rate limiter (1,000 req/s/IP)
                  └─▶ Prometheus + Grafana observability
```

All services are orchestrated via Docker Compose. Infrastructure (Zookeeper → Kafka → PostgreSQL 16 → Redis) starts before application services. Health checks use `/health` HTTP endpoints rather than TCP probes, guaranteeing application-level readiness.

### 3.2 Module 1 — C++ Limit Order Book Engine

**Technology choice:** C++20 instead of Python. Python's Global Interpreter Lock prevents true thread parallelism, making a 100k QPS target unachievable in Python without external processes. C++20 with `std::thread` and lock-free ring buffers achieves native multi-core utilisation.

**Data structures:** The order book uses `std::map` (Red-Black Tree) keyed on price per side — O(log P) insertion and O(1) best-price access. Each price level holds a FIFO `std::deque` for time-priority matching.

**Three-thread pipeline:**
- Thread A: Kafka consumer (`raw_orders`) → lock-free inbound ring buffer
- Thread B: Matching engine → outbound ring buffer
- Thread C: Kafka producer (`executed_trades`)

HTTP and WebSocket serving uses the Drogon C++ framework. Prometheus metrics (`lob_orders_total`, `lob_order_latency_ms`) are exposed at `/lob/metrics`.

### 3.3 Module 2 — TimescaleDB Time-Series Analytics

**Technology choice:** TimescaleDB over plain PostgreSQL. Financial tick data is append-only and almost always queried by time range. TimescaleDB's automatic chunk partitioning means a 1-hour query touches 1–2 chunks rather than the full table.

**Hypertable configuration:**
```sql
SELECT create_hypertable('raw_ticks', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    partitioning_column => 'symbol',
    number_partitions   => 4);
```
Chunks older than 7 days are compressed with the native columnar codec. Retention enforced at 90 days.

**Continuous aggregates:** Four materialised OHLCV views (`ohlcv_1m`, `ohlcv_5m`, `ohlcv_15m`, `ohlcv_1h`) auto-refresh on schedule. SQL indicator functions (`fn_vwap`, `fn_sma20`, `fn_bollinger`, `fn_rsi14`) query these aggregates for sub-5ms response times.

**Kafka ingestor:** Batches 1,000 records or 100ms, then uses `psycopg3` binary `COPY` to bulk-insert into `raw_ticks` — the fastest PostgreSQL bulk-insert path.

### 3.4 Module 3 — Apache AGE Graph + Bellman-Ford Arbitrage

**Technology choice:** Apache AGE (A Graph Extension for PostgreSQL) instead of a standalone graph database like Neo4j. AGE runs inside PostgreSQL, allowing a single transaction to perform a Cypher graph traversal and write the result to `arbitrage_signals` with no network round-trip.

**Graph schema:** `fx_graph` contains 20 `Asset` nodes (10 crypto + 10 fiat) connected by directed `EXCHANGE` edges with properties `{bid, ask, spread, last_updated}`.

**Bellman-Ford arbitrage detection:** Transforms edge weights as `w(i,j) = −log(rate(i,j))`. A negative cycle in the transformed graph corresponds to a profitable arbitrage cycle (where ∏ rates > 1.0). N−1 relaxation passes detect all shortest paths; an Nth pass detects negative cycles. At N=20 nodes with ~380 edges, one complete run takes <5ms.

**Cypher query interface:** The graph API exposes 3-hop cycle detection, shortest arbitrage path, high-spread edge detection, and a crypto-only subgraph endpoint — all backed by Cypher queries running inside PostgreSQL transactions.

### 3.5 Module 4 — Quantum Engine (Research Benchmark)

**Purpose:** Grover's Algorithm is implemented as a research benchmark alongside Bellman-Ford, not as a production replacement. Both algorithms run on the same rate matrix from `/graph/rates` and write their results to `arbitrage_signals`.

**Grover circuit:**
1. Enumerate all P(N,3) directed 3-cycles.
2. Qubit register: `n = ⌈log₂(|cycles|)⌉` qubits.
3. Hadamard gates for uniform superposition.
4. Oracle (phase-flip via MCX gate) + diffuser (`2|s⟩⟨s|−I`) for `⌊π/4·√|cycles|⌋` iterations.
5. 1,024-shot measurement on AerSimulator; decode top bitstring to a cycle.

**Why AerSimulator is slower:** AerSimulator maintains the full 2ⁿ-element complex state vector classically — O(2ⁿ) overhead per gate. On real quantum hardware with native MCX gate support, Grover achieves O(√N) oracle queries, a quadratic speedup over Bellman-Ford. The benchmark chart is the key research deliverable showing these scaling curves diverge as N increases.

### 3.6 Module 5 — Security Proxy & Observability

**SQL injection firewall:** Two-layer defence: (1) string scan against 15 `BANNED_PATTERNS`, (2) `sqlglot.parse()` AST walk detecting DDL node types (`Drop`, `Truncate`, `Create`, `AlterTable`). Blocked requests return HTTP 403 and are logged to `security_events`.

**Rate limiter:** Redis `INCR` + `EXPIRE` sliding-window per client IP. Blocks at >1,000 requests/second (HTTP 429). Falls back to an in-process token bucket if Redis is unavailable.

**Observability:** Prometheus scrapes all services every 15 seconds. Grafana provides a 49-panel dashboard including the CLASSICAL vs QUANTUM arbitrage signal comparison and QPS/p99 latency tracking.

### 3.7 Key Database Schema

```sql
-- Time-series tick storage (TimescaleDB hypertable)
CREATE TABLE raw_ticks (
    ts        TIMESTAMPTZ NOT NULL,
    symbol    TEXT NOT NULL,
    price     NUMERIC NOT NULL,
    volume    NUMERIC NOT NULL,
    side      CHAR(1),
    order_id  UUID,
    trade_id  UUID
);
SELECT create_hypertable('raw_ticks', 'ts', chunk_time_interval => INTERVAL '1 day',
    partitioning_column => 'symbol', number_partitions => 4);

-- Arbitrage signals (both algorithms write here)
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

-- Security audit log (TimescaleDB hypertable)
CREATE TABLE security_events (
    event_id   BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    client_ip  TEXT,
    event_type TEXT CHECK (event_type IN ('SQL_INJECTION', 'RATE_LIMIT', 'AUTH_FAIL')),
    raw_payload TEXT,
    blocked    BOOLEAN,
    endpoint   TEXT
);
SELECT create_hypertable('security_events', 'ts', chunk_time_interval => INTERVAL '1 day');
```

---

## 4. Results and Screenshots

### 4.1 LOB Engine — Throughput Benchmark

Siege was run with 200 concurrent users for 30 seconds:

| Metric | Result |
|--------|--------|
| Total Transactions | >3,000,000 |
| Throughput | **>100,000 QPS** |
| Availability | 100% |
| p99 Latency | **<10ms** |
| Failed Transactions | 0 |

### 4.2 TimescaleDB — Hypertable vs Plain PostgreSQL

1,000,000 GBM-generated rows loaded into both tables. Same OHLCV range query run 10 times each:

| Trial | Plain PostgreSQL (ms) | TimescaleDB Hypertable (ms) |
|-------|----------------------|----------------------------|
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
| **Average** | **353.1 ms** | **9.2 ms** |
| **Speedup** | — | **38×** |

Chunk exclusion eliminates 23 of 24 daily chunks for a 1-hour query, explaining the 38× speedup.

![TimescaleDB Benchmark](../module2_timescale/bench_out/benchmark_timescale.png)

### 4.3 Bellman-Ford Live Arbitrage Detection

- Runs every 500ms on the live 20-node AGE graph
- Execution time at N=20: **<5ms** per run (p99: 0.397ms in isolation)
- Sample detected signal: `USD → BTC → ETH → USD`, profit 0.0031%
- All signals stored in `arbitrage_signals` with `method='CLASSICAL'`

### 4.4 Quantum vs Classical Benchmark

| N nodes | BF mean (ms) | Grover mean (ms) | Ratio | Qubits | Circuit Depth |
|---------|-------------|-----------------|-------|--------|--------------|
| 4 | 0.005 | 4.1 | 826× | 6 | 27 |
| 8 | 0.032 | 26.7 | 834× | 10 | 486 |
| 12 | 0.093 | 168.9 | 1,816× | 12 | 2,034 |
| 16 | 0.208 | 550.3 | 2,645× | 13 | 4,878 |
| 20 | 0.392 | 1,914.9 | 4,884× | 14 | 9,972 |
| 24 | 0.718 | 5,036.6 | 7,014× | 15 | 16,587 |
| 28 | 1.164 | 12,806.4 | 11,002× | 16 | 24,831 |
| **32** | **3.481** | **20,373.9** | **5,848×** | **16** | **42,363** |

AerSimulator's state-vector model grows at O(2ⁿ) per gate — Grover's simulator overhead dominates. On real quantum hardware, the Grover curve would show O(√N) oracle queries and the lines would invert.

![Quantum Benchmark: Bellman-Ford vs Grover](../module4_quantum/bench_out/benchmark_quantum.png)

### 4.5 Security — SQL Injection Blocking

All 10 OWASP Top-10 SQL injection payloads blocked with HTTP 403. Example:

```
POST /lob/order
{"symbol":"BTC/USD'; DROP TABLE raw_ticks;--","side":"B","price":1,"quantity":1}
→ HTTP 403 Forbidden  (logged to security_events)
```

### 4.6 Grafana Dashboard — Live System Overview

The 49-panel Grafana dashboard at `http://localhost:3000` provides:

**Hero row (6 stat tiles):** LOB live throughput · TimescaleDB 38× speedup · arbitrage signals detected (24h) · Grover overhead at N=32 (5,848×) · SQL injections blocked · services online

**Price Analysis:** Candlestick OHLCV with SMA-20 overlay, RSI-14 indicator

**Graph Arbitrage Engine:** Bellman-Ford signal timeline (500ms cadence), profit distribution histogram, CLASSICAL vs QUANTUM comparison table with colour-coded columns

**Quantum Engine:** Full N=4→32 benchmark table (green = BF, purple = Grover, orange = ratio), circuit depth and qubit count stat tiles, AerSimulator overhead annotation

**Security & Observability:** SQL injection counter, rate-limit counter, QPS time series, p99 latency time series, recent security events log table

---

## 5. References

1. TimescaleDB Documentation. *Hypertables and Chunks*. https://docs.timescale.com/timescaledb/latest/how-to-guides/hypertables/
2. Apache AGE Documentation. *Graph Data Modeling in PostgreSQL*. https://age.apache.org/age-manual/master/intro/overview.html
3. Grover, L. K. (1996). *A fast quantum mechanical algorithm for database search*. Proceedings of the 28th Annual ACM Symposium on Theory of Computing (STOC), 212–219.
4. Bellman, R. (1958). *On a routing problem*. Quarterly of Applied Mathematics, 16(1), 87–90.
5. Confluent Documentation. *Apache Kafka: Producer and Consumer APIs*. https://developer.confluent.io/learn-kafka/
6. Qiskit Documentation. *AerSimulator and Statevector Simulation*. https://qiskit.org/ecosystem/aer/stubs/qiskit_aer.AerSimulator.html
7. sqlglot. *SQL Parser, Transpiler, and Optimizer*. https://sqlglot.com/
8. Drogon C++ Web Framework. https://github.com/drogonframework/drogon
9. Prometheus Documentation. *Metric Types*. https://prometheus.io/docs/concepts/metric_types/
10. Ford, L. R. (1956). *Network Flow Theory*. RAND Corporation Paper P-923.
