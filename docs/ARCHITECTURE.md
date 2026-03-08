# System Architecture Document

## Hybrid Trading Database System

---

## 1. High-Level Data Flow

```
[Binance/Alpha Vantage WebSocket]
          │
          ▼
   [Apache Kafka]  ←── raw_orders topic (4 partitions)
          │
    ┌─────┴──────────────────────────────────────┐
    │                                            │
    ▼                                            ▼
[Module 1: LOB Engine]                 [Module 2: TimescaleDB]
 C++20 Core (ashuwhy/lob submodule)     Hypertable: raw_ticks
 pybind11 Python bindings               Continuous Aggregates: ohlcv_1m/5m/15m/1h
 LMAX ring buffer + 3-thread pipeline   SQL Indicators: VWAP, SMA20, Bollinger, RSI14
 FastAPI REST + WebSocket API                    │
    │                                            │
    │ best-bid rates (every 500ms)               │
    ▼                                            │
[Module 3: Apache AGE Graph]                     │
 Directed weighted fx_graph                      │
 20+ Asset nodes, EXCHANGE edges                 │
 Cypher arbitrage path queries                   │
    │                                            │
    ▼                                            │
[Module 4: Qiskit Quantum Engine]                │
 Grover's Algorithm O(√N)                        │
 Writes → arbitrage_signals table                │
    │                                            │
    └─────────────┬──────────────────────────────┘
                  ▼
       [Module 5: FastAPI Security Proxy :8000]
        SQL injection AST firewall (sqlglot)
        Redis sliding-window rate limiter (1000 req/s/IP)
        Prometheus metrics exposition
                  │
                  ▼
        [Prometheus :9090 + Grafana :3000]
         5-panel live dashboard
         postgres-exporter, redis-exporter, node-exporter
```

---

## 2. Component Inventory

| Component | Technology | Port | Owned By |
|-----------|-----------|------|---------|
| Market data ingestor | Python WebSocket client | — | -- |
| Kafka broker | Apache Kafka 7.6.0 (Confluent) | 9092 | -- |
| LOB matching engine | C++20 Core (`ashuwhy/lob`) + Python 3.12 FastAPI wrapper | 8001 | -- |
| TimescaleDB | PostgreSQL 16 + TimescaleDB 2.x | 5432 | -- |
| Apache AGE graph layer | PostgreSQL 16 + AGE extension (same PG instance) | 5432 | -- |
| Qiskit quantum engine | Python 3.12 + Qiskit 0.45 + AerSimulator | 8004 | -- |
| Security proxy | FastAPI + sqlglot + Redis 7 | 8000 (public) | -- |
| Redis | Redis 7-alpine | 6379 | -- |
| Prometheus | Prometheus 2.48 | 9090 | -- |
| Grafana | Grafana 10.3 | 3000 | -- |
| postgres-exporter | prometheuscommunity/postgres-exporter | 9187 | -- |
| redis-exporter | oliver006/redis_exporter | 9121 | -- |
| node-exporter | prom/node-exporter | 9100 | -- |

---

## 3. LOB Engine – External Submodule

### 3.1 Source Repository

```
Git Submodule: https://github.com/ashuwhy/lob
Mount path:    module1_lob/engine/
```

### 3.2 C++ Core Components (from submodule)

| File | Purpose |
|------|---------|
| `cpp/src/book_core.cpp` | Main matching engine — Red-Black Tree + FIFO queues |
| `cpp/src/price_levels.cpp` | Price-level management |
| `cpp/include/lob/mempool.hpp` | Arena allocator / memory pool |
| `cpp/src/replay.cpp` | TAQ event replay engine |
| `cpp/src/taq_writer.cpp` | Trade-and-quote writer |
| `python/olob/_bindings.cpp` | pybind11 bridge → Python `olob` module |

### 3.3 Python Package

```python
from olob import OrderBook, Side, OrderType   # compiled C++ via pybind11
book = OrderBook()
book.add_limit_order(side=Side.BUY, price=65000.0, qty=0.5)
```

### 3.4 Benchmark Baseline (pre-existing)

- `bench_out/latencies.csv` — nanosecond-level latency measurements
- `bench_out/latency_histogram.png` — distribution chart
- Target: > 100,000 order ops/sec at p99 < 10ms

---

## 4. Inter-Module Contracts

### 4.1 LOB Engine → TimescaleDB

- **Method:** Batch INSERT via COPY protocol (`psycopg3 copy()`)
- **Trigger:** Every 100ms or 1,000 trades (whichever comes first)
- **Payload:** `(ts, symbol, price, volume, side, order_id, trade_id)`

### 4.2 LOB Engine → Apache AGE

- **Method:** Background asyncio worker polls `GET /lob/depth/{symbol}` every 500ms
- **Action:** `MATCH … SET r.bid = $new_bid` via AGE Cypher
- **Latency target:** Edge weight updated within 600ms of LOB price change

### 4.3 Apache AGE → Qiskit Engine

- **Method:** Python function call (`run_grover(rates_matrix, nodes)`) or `POST /quantum/run-grover`
- **Payload:** N×N float adjacency matrix (exchange rates)
- **Frequency:** Every 1 second (or on-demand)

### 4.4 Qiskit Engine → PostgreSQL

- **Method:** `psycopg3` INSERT into `arbitrage_signals`
- **Payload:** `(ts, path, profit_pct, circuit_depth, grover_iterations, classical_ms, quantum_ms, graph_size_n, method)`

### 4.5 All Modules → FastAPI Security Proxy

- **Method:** All external HTTP/WS requests routed through port 8000
- **Security layers:** Rate-limit check → SQL AST validation → forward to internal service

### 4.6 Prometheus Scraping

| Target | Endpoint | Interval |
|--------|---------|---------|
| FastAPI proxy | `hqt-fastapi:8000/metrics` | 15s |
| LOB engine | `hqt-lob:8001/metrics` | 15s |
| postgres-exporter | `postgres-exporter:9187` | 15s |
| redis-exporter | `redis-exporter:9121` | 15s |
| node-exporter | `node-exporter:9100` | 15s |

---

## 5. Concurrency Model (Module 1)

```
Thread A: InboundThread
  └── confluent_kafka.Consumer on 'raw_orders'
  └── Writes OrderEvent → LMAX Ring Buffer (2^20 slots)

Thread B: MatchingThread
  └── Reads from Ring Buffer
  └── Calls C++ OrderBook via pybind11 (olob)
  └── Emits TradeEvent → output buffer

Thread C: PersistenceThread
  └── Reads TradeEvents (batch ≤ 1000 or 100ms timeout)
  └── psycopg3 binary COPY → raw_ticks + trades (TimescaleDB)
```

Ring buffer: fixed `2^20`-slot array, sequence-number lock-free (no mutex).

---

## 6. Docker Compose Service Graph

```
zookeeper ──► kafka ──► kafka-setup (one-shot topic creator, exits 0)
                  │
                  ├──► lob-engine       :8001  (depends_on: kafka, postgres)
                  └──► data-ingestor          (depends_on: kafka, postgres)

postgres  ──► quantum-engine  :8004  (depends_on: postgres)
          └──► lob-engine

postgres + redis + lob-engine + quantum-engine ──► fastapi-proxy :8000

fastapi-proxy ──► prometheus :9090
postgres-exporter :9187 ──► prometheus
redis-exporter    :9121 ──► prometheus
node-exporter     :9100 ──► prometheus

prometheus ──► grafana :3000
```

---

## 7. Security Architecture

```
External Request (port 8000)
      │
      ▼
Middleware 1 – Redis Sliding-Window Rate Limiter
  └── key: f"rl:{client_ip}"  window: 1s  limit: 1000 req
  └── Exceeded → HTTP 429 + INSERT security_events (RATE_LIMIT)
  └── Redis down → fallback in-process token bucket
      │
      ▼
Middleware 2 – SQL Injection AST Firewall (sqlglot)
  └── Scans: request body + query params
  └── Banned pattern match OR sqlglot AST detects DROP/TRUNCATE/UNION SELECT/xp_/EXEC
  └── Blocked → HTTP 403 + INSERT security_events (SQL_INJECTION)
      │
      ▼
Proxy forward to internal service router
  ├── /lob/*        → hqt-lob:8001
  ├── /analytics/*  → module2 router (same process)
  ├── /graph/*      → module3 router (same process)
  └── /quantum/*    → quantum-engine:8004
```

Banned SQL patterns: `DROP`, `TRUNCATE`, `UNION SELECT`, `--`, `/*`, `xp_`, `EXEC`, `INSERT INTO information_schema`

---

## 8. Known Issues Fixed (vs. Initial Codebase)

| File | Bug | Fix |
|------|-----|-----|
| `init.sql` | `ohlcv_5m`, `ohlcv_15m`, `ohlcv_1h` missing | Added all 3 CAs with refresh policies |
| `init.sql` | `trades` PK was `(trade_id, ts)` composite — FK refs fail | Changed to `PRIMARY KEY (trade_id)` |
| `init.sql` | `arbitrage_signals` PK was `(signal_id, ts)` composite | Changed to `PRIMARY KEY (signal_id)` |
| `init.sql` | `security_events` PK was `(event_id, ts)` composite | Changed to `PRIMARY KEY (event_id)` |
| `docker-compose.yml` | 7 services missing | Added all 7 |
| `prometheus.yml` | Scraped non-existent containers | Fixed after adding services |
| Project root | `.env.example` missing | Created |
| Project root | `raw_orders` Kafka topic never auto-created | Added `kafka-setup` one-shot container |
