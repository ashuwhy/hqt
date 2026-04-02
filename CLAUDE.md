# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Context

**HQT (Hybrid Quantum Trading)** is a CS39006 DBMS Lab project (IIT Kharagpur, Spring 2026) with a hard deadline of **April 15, 2026**. It implements a five-module trading database system with real-time order matching, time-series analytics, graph-based arbitrage detection, quantum algorithm benchmarking, and security observability.

**Module owners:**
- Module 1 (LOB Engine - C++20): Ashutosh Sharma
- Module 2 (TimescaleDB Analytics - Python): Sujal Anil Kaware
- Module 3 (Graph/Arbitrage - Python): Parag Mahadeo Chimankar
- Module 4 (Quantum Engine - Python/Qiskit): Kshetrimayum Abo
- Module 5 (Security/Observability - Python): Kartik Pandey

---

## Common Commands

### Docker (Primary Development Environment)

```bash
docker compose up -d                      # Start all services
docker compose logs -f <service>          # Tail logs for a service
docker compose down                       # Tear down
docker compose build <service>            # Rebuild a single service image
```

First run builds `Dockerfile.postgres` (TimescaleDB + Apache AGE); allow 3–5 minutes.

### Python Environment

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pre-commit install
```

### Linting

```bash
pre-commit run --all-files    # Run all pre-commit hooks
ruff check .                  # Check style
ruff format .                 # Auto-format
```

### Testing

```bash
pytest tests/                                              # Full suite
pytest -m unit                                             # Fast offline tests
pytest -m integration                                      # Requires Docker services up
pytest -m benchmark                                        # Performance tests (slow)
pytest tests/test_bellman_ford.py -v                       # Single test file
pytest tests/test_quantum.py::test_grover_basic -v         # Single test function
```

Pytest config lives in `pyproject.toml`: asyncio mode is `auto`, markers are `unit`, `integration`, `benchmark`.

### Benchmarks

```bash
python tests/run_all_benchmarks.py                         # All benchmarks
siege -c 200 -t 30S -f module1_lob/urls.txt               # LOB HTTP benchmark (requires Siege)
```

---

## Architecture Overview

### Service Topology and Data Flow

```
[Binance/Alpha Vantage WebSocket]
    ↓
[Kafka :9092]  ←  raw_orders topic
    ├─→ Module 1: LOB Engine (C++/Drogon, :8001) → executed_trades topic
    └─→ Module 2: TimescaleDB Ingestor (Python, :8002) → raw_ticks hypertable

Module 3: Graph Service (Python, :8003)
    ├─→ Polls LOB /depth every 500ms → updates Apache AGE edges
    └─→ Bellman-Ford every 500ms → arbitrage_signals (method='CLASSICAL')

Module 4: Quantum Engine (Python/Qiskit, :8004)
    ├─→ Polls Module 3 /graph/rates every 10s
    └─→ Grover's Algorithm benchmark → arbitrage_signals (method='QUANTUM')

Module 5: Security Proxy (Python/FastAPI, :8000)  ← ALL external traffic
    ├─→ SQL injection firewall (sqlglot AST)
    ├─→ Rate limiter (Redis sliding-window, 1000 req/s/IP)
    └─→ Routes /lob/* → :8001, /graph/* → :8003, /analytics/* → :8002, /quantum/* → :8004

[Prometheus :9090] ← scrapes all services every 15s
[Grafana :3000]    ← 5-panel dashboard (candlestick, LOB heatmap, volume, arbitrage signals, latency)
```

### Key Architectural Decisions

**ADR 1 - Bellman-Ford is production; Grover is research-only benchmark.**
Both algorithms insert to `arbitrage_signals` with `method='CLASSICAL'` or `method='QUANTUM'`. The Grafana Panel 4 renders both streams colour-coded. Grover runs on AerSimulator (classical state-vector), so its O(2^N) overhead vs BF's flat O(N·E) is expected and documented - this is the key comparative result.

**ADR 2 - LOB uses C++/Drogon (Tier 1), not Python/FastAPI (Tier 3).**
The LOB engine must sustain >100k order ops/sec at p99 <10ms. Python's GIL makes this impossible. All other modules remain Python. C++ files are under `module1_lob/`; the core matching engine is in the `engine/` git submodule.

### Database Schema (PostgreSQL 16 + TimescaleDB + Apache AGE)

**Key tables:**
- `raw_ticks` - TimescaleDB hypertable (chunk by 1 day, partition by 4 symbols, compress after 7d, retain 90d)
- `ohlcv_1m/5m/15m/1h` - continuous aggregates auto-refreshed from `raw_ticks`
- `orders`, `trades` - LOB state and executed trades
- `arbitrage_signals` - both BF and Grover results; `method` column distinguishes them
- `security_events` - TimescaleDB hypertable for SQL_INJECTION / RATE_LIMIT / AUTH_FAIL events
- `benchmark_runs` - records from all performance benchmarks
- Apache AGE graph `fx_graph` - 20 `Asset` nodes (10 crypto + 10 fiat), directed `EXCHANGE` edges with `bid`, `ask`, `spread`, `last_updated`

Full DDL is in `init.sql` and documented in `docs/DATABASE_SCHEMA.md`.

### Module 1 - LOB Engine (C++20)

Three-thread pipeline:
1. **Thread A**: Kafka consumer (`raw_orders`) → lock-free ring buffer
2. **Thread B**: Matching engine (Red-Black Tree price levels, FIFO deque per level) → outbound queue
3. **Thread C**: Kafka producer (`executed_trades`)

Prometheus metrics: `lob_orders_total`, `lob_trades_total`, `lob_order_latency_ms`, `lob_active_orders`.

### Module 5 - Security Proxy (Python/FastAPI)

Acts as the single entry point on `:8000`. `sql_firewall.py` uses sqlglot to parse ASTs for injection patterns before forwarding requests. The rate limiter (`rate_limiter.py`) uses Redis sliding-window and falls back to an in-process token bucket if Redis is unavailable.

---

## Test Structure

| File | Scope | Marker |
|------|-------|--------|
| `test_bellman_ford.py` | BF algorithm correctness, cycle detection | `@unit` |
| `test_quantum.py` | Grover circuit, oracle, diffuser | `@unit`, `@benchmark` |
| `test_security.py` | SQL injection blocking, rate limiter | `@unit`, `@integration` |
| `test_lob.py` | Order placement, cancel, depth queries | `@integration` |
| `test_timescale.py` | OHLCV queries, indicator correctness | `@integration` |
| `test_graph.py` | AGE graph ops, edge updates | `@integration` |
| `test_integration_e2e.py` | Full flow: order → tick → arbitrage signal | `@integration` |

Shared fixtures (DB connection, service clients, symbol generator) are in `tests/conftest.py`.

---

## Environment Variables

See `.env.example`. Key variables:
- `POSTGRES_USER/PASSWORD/DB/HOST`
- `REDIS_URL` - `redis://redis:6379`
- `KAFKA_BOOTSTRAP_SERVERS` - `kafka:9092`
- `LOB_ENGINE_URL`, `QUANTUM_ENGINE_URL`
- `ALPHA_VANTAGE_API_KEY` - required for live market data

---

## CI/CD

`.github/workflows/docker-publish.yml` builds and publishes images for all six services to GHCR on push to `dev`, PRs, and daily at 16:32 UTC. Images are signed with Cosign on non-PR pushes. Matrix strategy builds each service independently.

---

## Key Documentation

- `docs/ARCHITECTURE.md` - full data flow, module contracts, concurrency model
- `docs/DATABASE_SCHEMA.md` - complete DDL and index rationale
- `docs/MODULE_SPECS.md` - per-module implementation details, endpoints, benchmarks
- `docs/PRD.md` - goals (G1–G7) and success metrics
- `docs/TASK_LIST.md` - granular task breakdown with owner and status
