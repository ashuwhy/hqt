# System Architecture Document

## Hybrid Trading Database System

---

## 1. High-Level Data Flow

```
[Binance / Alpha Vantage WebSocket]
          │
          ▼
   [Apache Kafka :9092]  ◄── raw_orders topic (4 partitions)
          │
    ┌─────┴──────────────────────────────────────┐
    │                                            │
    ▼                                            ▼
[Module 1: LOB Engine :8001]           [Module 2: TimescaleDB :8002]
 C++20 Core (ashuwhy/lob submodule)     Hypertable: raw_ticks
 pybind11 Python bindings (olob)        Continuous Aggregates: ohlcv_1m/5m/15m/1h
 LMAX ring buffer, 3-thread pipeline    SQL Indicators: VWAP, SMA20, Bollinger, RSI14
 FastAPI REST + WebSocket API           FastAPI analytics router
    │                                            │
    │ best-bid / best-ask (every 500ms)          │
    ▼                                            │
[Module 3: Graph Layer - Apache AGE]             │
 Directed weighted fx_graph (20+ nodes)          │
 *** Bellman-Ford PRIMARY arbitrage ***          │
 Cypher graph query interface                    │
 Writes method='CLASSICAL' signals every 500ms  │
    │                                            │
    │ rate matrix JSON (/graph/rates)            │
    ▼                                            │
[Module 4: Quantum Engine :8004]                 │
 *** Grover's Algorithm RESEARCH ONLY ***        │
 Runs every 10s for benchmarking only           │
 Writes method='QUANTUM' signals                │
 benchmark_quantum.png: BF flat / Grover exp.   │
    │                                            │
    └─────────────┬──────────────────────────────┘
                  ▼
       [Module 5: FastAPI Security Proxy :8000]
        SQL injection AST firewall (sqlglot)
        Redis sliding-window rate limiter (1000 req/s/IP)
        Prometheus metrics at /metrics
                  │
                  ▼
        [Prometheus :9090 + Grafana :3000]
         5-panel dashboard (Panel 4 shows CLASSICAL + QUANTUM signals)
         postgres-exporter :9187 | redis-exporter :9121 | node-exporter :9100
```

---

## 2. Architecture Decision Record - Bellman-Ford vs Quantum

**Decision Date:** March 9, 2026
**Status:** ACCEPTED - applies to Modules 3, 4, 5, and the final report

### Decision

Bellman-Ford is the **primary production arbitrage algorithm**.
Grover's Algorithm (Qiskit AerSimulator) is a **research benchmark only**.

### Rationale

| Criterion | Bellman-Ford | Grover (AerSimulator) |
|-----------|-------------|----------------------|
| Correctness | 100% deterministic | ~50–85% probabilistic |
| Speed at N=20 | < 5 ms | ~10,000 ms |
| Finds ALL cycles | Yes | No (most probable only) |
| Production viable | Yes | No (simulation overhead inverts advantage) |
| Academic value | High - graph algorithms | High - complexity theory proof |

### Implementation Rule

- **Bellman-Ford** runs every 500ms inside `quantum_service.py` background loop; inserts `method='CLASSICAL'` rows into `arbitrage_signals`
- **Grover** runs every 10s in the same service for benchmarking only; inserts `method='QUANTUM'` rows
- Both write to the same `arbitrage_signals` table, distinguished by the `method` column
- Grafana Panel 4 displays both streams, colour-coded
- `benchmark_quantum.png` - BF as the near-flat line and Grover rising exponentially - is the **expected, documented result** and becomes the strongest slide in the report

---

## 3. Architecture Decision Record - LOB Network Layer (Python vs C++)

**Decision Date:** March 9, 2026
**Status:** ACCEPTED - Python FastAPI retained for Phase 1; optimizations documented for future pivot

### Three Tiers of LOB Network Performance

| Tier | Approach | Est. QPS | Complexity | Decision |
|------|----------|---------|-----------|---------|
| **Tier 1** | C++ network layer (uWebSockets / Drogon) | **100,000+** | Very high - rewrite `lob_api.py` → `lob_api.cpp`; packets arrive directly into C++ memory | Future HFT pivot |
| **Tier 2** | Persistent WebSocket / TCP socket (binary frames) | **10,000+** | Medium - eliminate per-request HTTP handshake overhead; stream binary data over single open connection | Future optimisation |
| **Tier 3** | Python FastAPI + quick wins | **5,000+** | Low - two targeted changes to existing code | **Current implementation** |

### Why We Are Moving to C++ for Phase 1 (Tier 1)

The bottleneck in a Python setup is **network I/O, not the C++ matching math**. While Python FastAPI provides a good starting point, real HFT systems require nanosecond-level packet handling. By rewriting the network layer in C++ using `uWebSockets` (or similar), packets arrive directly into C++ memory, get matched, and respond in microseconds. This is how real crypto exchanges (Binance, OKX) are built.

This pivot alters some architectural boundaries:

1. **Network**: REST and WebSocket APIs are now served directly by C++ (`uWebSockets`)
2. **Ingestion**: The C++ engine consumes `raw_orders` directly from Kafka using `librdkafka`
3. **Persistence**: Instead of Python `psycopg3.copy()` inside the matching engine, the C++ engine publishes matched trades to a new `executed_trades` Kafka topic. Module 2 (`data-ingestor`) consumes this and writes to TimescaleDB.
4. **Metrics**: Prometheus metrics are natively exposed via `prometheus-cpp`

### Tier 1: Full C++ Network Layer (HFT Pivot)

We will build the module using the following pattern:

```cpp
// lob_server.cpp - Crow C++ framework server calling C++ OrderBook directly
#include "crow.h"
#include "lob/book_core.hpp"

// ... Kafka consumer thread for raw_orders ...
// ... Kafka producer thread for executed_trades ...

int main() {
    crow::SimpleApp app;

    CROW_ROUTE(app, "/lob/order").methods(crow::HTTPMethod::POST)
    ([&book](const crow::request& req) {
        Order o = parse_json(req.body);        // zero-copy parse
        auto trades = book.place(o);           // nanosecond match
        return crow::response(201, to_json(trades));
    });

    app.port(8001).multithreaded().run();
}
```

**Conclusion:** We are pivoting to the Tier 1 C++ implementation for the LOB engine.

---

## 4. Component Inventory

| Component | Technology | Port | Owner |
|-----------|-----------|------|-------|
| Market data ingestor | Python WebSocket client | - | Member 2 |
| Kafka broker | Confluent CP-Kafka 7.6.0 | 9092 | Member 2 |
| Zookeeper | Confluent CP-Zookeeper 7.6.0 | 2181 | Member 2 |
| LOB matching engine | C++20 core + uWebSockets + librdkafka | 8001 | Member 1 |
| TimescaleDB analytics | PostgreSQL 16 + TimescaleDB 2.x + FastAPI router | 5432 / 8002 | Member 2 |
| Apache AGE graph layer | PostgreSQL 16 + AGE (same PG instance) | 5432 | Member 3 |
| Quantum engine | Python 3.12 + Qiskit 0.45 + AerSimulator | 8004 | Member 4 |
| Security proxy | FastAPI + sqlglot + Redis 7 | 8000 (public) | Member 5 |
| Redis | Redis 7-alpine | 6379 | Member 5 |
| Prometheus | Prometheus 2.48 | 9090 | Member 5 |
| Grafana | Grafana 10.3 | 3000 | Member 5 |
| postgres-exporter | prometheuscommunity/postgres-exporter | 9187 | Member 5 |
| redis-exporter | oliver006/redis_exporter | 9121 | Member 5 |
| node-exporter | prom/node-exporter | 9100 | Member 5 |

---

## 5. LOB Engine - External Git Submodule

### 4.1 Source Repository

```
Submodule URL: https://github.com/ashuwhy/lob
Mount path:    module1_lob/engine/
```

### 4.2 C++ Core Files

| File | Purpose |
|------|---------|
| `cpp/src/book_core.cpp` | Matching engine - Red-Black Tree + FIFO queues per price level |
| `cpp/src/price_levels.cpp` | Price-level management |
| `cpp/include/lob/mempool.hpp` | Arena allocator / lock-free memory pool |
| `cpp/src/replay.cpp` | TAQ event replay |
| `cpp/src/taq_writer.cpp` | Trade-and-quote writer |
| `python/olob/_bindings.cpp` | pybind11 bridge → Python `olob` module |

### 4.3 Confirming pybind11 Class Names

Before writing `lob_api.py`, verify actual exposed names:

```bash
grep "py::class_" module1_lob/engine/python/olob/_bindings.cpp
```

Expected: `OrderBook` / `NewOrder` / `bk.poll_trades()` - align API code to actual names.

### 4.4 Pre-existing Benchmark Baseline

- `bench_out/latencies.csv` - nanosecond-level measurements
- `bench_out/latency_histogram.png` - embed in final report
- Target: > 100,000 order ops/sec at p99 < 10ms

---

## 6. Inter-Module Contracts

### 5.1 LOB Engine → TimescaleDB

- **Method:** `librdkafka` C++ Producer to `executed_trades` Kafka topic. Module 2 Python `data-ingestor` consumes this topic and uses `psycopg3.copy()` binary COPY.
- **Trigger:** Real-time push to Kafka
- **Payload:** `(ts, symbol, price, volume, side, order_id, trade_id)` → `raw_ticks`

### 5.2 LOB Engine → Apache AGE

- **Method:** `edge_weight_updater.py` asyncio loop, 500ms interval
- **Query:** `GET http://lob-engine:8001/lob/depth/{symbol}`
- **Action:** Cypher `MATCH (a)-[r:EXCHANGE]->(b) SET r.bid=$bid, r.ask=$ask, r.last_updated=timestamp()`
- **Latency target:** Edge updated within 600ms of LOB price change

### 5.3 Apache AGE → Bellman-Ford (Primary - every 500ms)

- **Method:** `build_rate_matrix(conn)` queries all AGE `EXCHANGE` edges via psycopg3
- **Output:** INSERT `arbitrage_signals` with `method='CLASSICAL'`

### 5.4 Apache AGE → Grover (Benchmark - every 10s)

- **Method:** `GET /graph/rates` returns N×N float adjacency matrix JSON
- **Output:** INSERT `arbitrage_signals` with `method='QUANTUM'`

### 5.5 All Modules → FastAPI Security Proxy

- All external traffic enters only through port 8000
- Middleware order: rate-limit check → SQL AST validation → proxy to internal service

### 5.6 Prometheus Scraping (15s interval)

| Job | Target | Port |
|----|--------|------|
| `fastapi` | `hqt-fastapi:8000/metrics` | 8000 |
| `lob-engine` | `hqt-lob:8001/metrics` | 8001 |
| `postgres` | `postgres-exporter:9187` | 9187 |
| `redis` | `redis-exporter:9121` | 9121 |
| `node` | `node-exporter:9100` | 9100 |

---

## 7. Concurrency Model (Module 1 - C++)

```
Thread A - InboundThread
  └── librdkafka Consumer on 'raw_orders'
  └── Writes OrderEvent → Lock-free Queue / Ring Buffer

Thread B - MatchingThread
  └── Reads from inbound queue
  └── Calls C++ OrderBook
  └── Emits TradeEvent → Outbound queue
  └── Evaluates uWebSockets broadcast for depth updates

Thread C - PersistenceThread (Outbound)
  └── Reads TradeEvents
  └── librdkafka Producer → 'executed_trades' Kafka topic
```

---

## 8. Docker Compose Service Graph

```
zookeeper ──► kafka ──► kafka-setup (one-shot, creates raw_orders topic, exits 0)
                  │
                  ├──► lob-engine       :8001  (depends_on: kafka, postgres)
                  └──► data-ingestor    :8002  (depends_on: kafka, postgres)

postgres  ──► quantum-engine  :8004  (depends_on: postgres)
          └──► lob-engine

postgres + redis + lob-engine + quantum-engine ──► fastapi-proxy :8000
  ⚠ fastapi-proxy depends on quantum-engine: service_started
    (NOT service_healthy - quantum /health was missing in placeholder)

fastapi-proxy      ──► prometheus :9090
postgres-exporter  ──► prometheus
redis-exporter     ──► prometheus
node-exporter      ──► prometheus

prometheus ──► grafana :3000
```

### Known docker-compose Bugs (fix in Phase 0)

- `redis-exporter` missing `REDIS_ADDR: redis://redis:6379` env var → Prometheus target DOWN
- `postgres-exporter` `DATA_SOURCE_NAME` missing `?sslmode=disable` → SSL error
- `lob-engine`, `data-ingestor`, `quantum-engine`, `fastapi-proxy` all missing `env_file: - .env`

---

## 9. Security Architecture

```
External Request → port 8000
      │
      ▼
Middleware 1 - Redis Sliding-Window Rate Limiter
  INCR rl:{ip} + EXPIRE 1s | limit 1,000 req/s/IP
  Blocked  → HTTP 429 + INSERT security_events (RATE_LIMIT)
  Redis down → fallback to in-process threading.Semaphore token bucket
      │
      ▼
Middleware 2 - SQL Injection AST Firewall (sqlglot)
  sqlglot.parse() walks AST for DDL node types (Drop, Truncate, Create)
  String scan: DROP | TRUNCATE | UNION SELECT | -- | /* | xp_ | EXEC | information_schema
  Blocked  → HTTP 403 + INSERT security_events (SQL_INJECTION)
      │
      ▼
Proxy router
  /lob/*        → hqt-lob:8001        (HTTP reverse proxy)
  /analytics/*  → analytics_api router (same process, module2)
  /graph/*      → graph_api router     (same process, module3)
  /quantum/*    → quantum-engine:8004  (HTTP reverse proxy)
  /admin/*      → admin router         (security_events + benchmark_runs queries)
  /health       → upstream health checks
  /metrics      → Prometheus generate_latest()
```
