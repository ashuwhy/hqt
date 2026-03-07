# System Architecture Document
## Hybrid Trading Database System

---

## 1. High-Level Data Flow

```
[Binance/Alpha Vantage WebSocket]
          │
          ▼
   [Apache Kafka]  ←── raw_orders topic
          │
    ┌─────┴──────────────────────────────────────┐
    │                                            │
    ▼                                            ▼
[Module 1: LOB Engine]                 [Module 2: TimescaleDB]
 Red-Black Tree + LMAX Disruptor        Hypertable: raw_ticks
 3-thread pipeline                      Continuous Aggregates: OHLCV
 REST + WebSocket API                   SQL Indicators: VWAP, SMA, Bollinger, RSI
    │                                            │
    │ best-bid rates (every 500ms)               │
    ▼                                            │
[Module 3: Apache AGE Graph]                     │
 Directed weighted graph                         │
 Cypher arbitrage path queries                   │
    │                                            │
    ▼                                            │
[Module 4: Qiskit Quantum Engine]                │
 Grover's Algorithm O(√N)                        │
 Writes → arbitrage_signals table                │
    │                                            │
    └─────────────┬──────────────────────────────┘
                  ▼
       [Module 5: FastAPI Security Proxy]
        SQL injection AST firewall (sqlglot)
        Redis sliding-window rate limiter
                  │
                  ▼
        [Prometheus + Grafana]
         5-panel live dashboard
```

---

## 2. Component Inventory

| Component | Technology | Port | Owned By |
|-----------|-----------|------|---------|
| Market data ingestor | Python WebSocket client | — | Member 2 |
| Kafka broker | Apache Kafka 3.x | 9092 | Member 2 |
| LOB matching engine | Python 3.12 / Java 21 | 8001 | Member 1 |
| TimescaleDB | PostgreSQL 16 + TimescaleDB 2.x | 5432 | Member 2 |
| Apache AGE graph layer | PostgreSQL 16 + AGE extension | 5432 (same PG) | Member 3 |
| Qiskit quantum engine | Python 3.12 + Qiskit 1.x | 8004 (internal) | Member 4 |
| Security proxy | FastAPI + sqlglot + Redis 7 | 8000 (public) | Member 5 |
| Redis | Redis 7 | 6379 | Member 5 |
| Prometheus | Prometheus 2.x | 9090 | Member 5 |
| Grafana | Grafana 10 | 3000 | Member 5 |
| Frontend (optional) | Next.js 15 | 3001 | Optional |

---

## 3. Inter-Module Contracts

### 3.1 LOB Engine → TimescaleDB
- **Method:** Batch INSERT via COPY protocol (psycopg3 `copy()`)
- **Trigger:** Every 100ms or 1000 trades (whichever first)
- **Payload:** `(ts, symbol, price, volume, side, order_id, trade_id)`

### 3.2 LOB Engine → Apache AGE
- **Method:** Background worker polls LOB best-bid every 500ms
- **Action:** `UPDATE` edge weight in `fx_graph` using Cypher via AGE

### 3.3 Apache AGE → Qiskit Engine
- **Method:** Python function call (same process or REST `/run-grover`)
- **Payload:** Adjacency matrix of size N×N (exchange rate floats)
- **Frequency:** Every 1 second

### 3.4 Qiskit Engine → PostgreSQL
- **Method:** psycopg3 INSERT into `arbitrage_signals` table
- **Payload:** `(ts, path, profit_pct, circuit_depth, classical_baseline_ms, quantum_ms)`

### 3.5 All modules → FastAPI Proxy
- **Method:** All external HTTP requests routed through port 8000
- **Security:** sqlglot AST validation on every query parameter

### 3.6 Prometheus Scraping
- **Targets:** FastAPI `/metrics` endpoint, PostgreSQL exporter, Redis exporter, Node exporter
- **Interval:** 15 seconds

---

## 4. Concurrency Model (Module 1)

```
Thread A: Inbound Thread
  └── Reads from Kafka → writes to LMAX Ring Buffer

Thread B: Matching Thread  
  └── Reads from Ring Buffer → executes LOB logic (Red-Black Tree)
  └── Writes matched trades to output buffer

Thread C: Persistence Thread
  └── Reads matched trades → bulk COPY to TimescaleDB
```

Ring buffer size: 2^20 slots (power-of-two for cache alignment)  
Lock-free: Uses sequence numbers + memory barriers (no mutex)

---

## 5. Docker Compose Service Graph

```yaml
services:
  kafka:          # depends_on: zookeeper
  zookeeper:
  postgres:       # TimescaleDB + AGE on same instance
  redis:
  lob-engine:     # depends_on: kafka, postgres
  data-ingestor:  # depends_on: kafka
  quantum-engine: # depends_on: postgres
  fastapi-proxy:  # depends_on: postgres, redis, lob-engine
  prometheus:     # depends_on: fastapi-proxy
  grafana:        # depends_on: prometheus, postgres
```

---

## 6. Security Architecture

```
External Request
      │
      ▼
FastAPI Middleware Stack:
  1. IP fingerprinting → Redis rate-limit check
     └── Exceeded? → HTTP 429, log to security_events
  2. SQL AST parser (sqlglot)
     └── Banned pattern detected? → HTTP 403, log to security_events
  3. Request forwarded to internal service
```

Banned SQL patterns: `DROP`, `TRUNCATE`, `UNION SELECT`, `--`, `/*`, `xp_`, `EXEC`, `INSERT INTO information_schema`
