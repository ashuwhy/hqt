# Granular Task List - Final

## Hybrid Trading Database System

> **Status as of March 9, 2026**
> Phase 0 ✅ Complete (3 bugs to fix) | Phase 1 ✅ Complete (3 bugs + 2 verifications) | Phases 2–6 pending
> **Deadline: April 15, 2026**

---

## Status Key

- `[x]` Done
- `[ ]` Not started
- `⚠️` Bug confirmed in completed work - fix before building on top

---

## PHASE 0 - Infrastructure ✅ COMPLETE

All 15 tasks done. Three bugs found post-completion - fix immediately before Phase 2.

### Open Bugs

- [x] **[BUG-P0-1]** `docker-compose.yml` `redis-exporter` service missing `REDIS_ADDR: redis://redis:6379` env var → Prometheus redis target shows DOWN permanently
- [x] **[BUG-P0-2]** `docker-compose.yml` `postgres-exporter` `DATA_SOURCE_NAME` missing `?sslmode=disable` → exporter exits with SSL handshake error
- [x] **[BUG-P0-3]** `docker-compose.yml` services `lob-engine`, `data-ingestor`, `quantum-engine`, `fastapi-proxy` all missing `env_file: - .env` → environment variables not injected at runtime

---

## PHASE 1 - Module 1: LOB Engine ✅ COMPLETE

All 14 tasks done. Three bugs found - fix before running benchmarks.

### Open Bugs

- [x] **[BUG-P1-1]** `lob_api.py` obsolete (C++ rewrite removed the Python bug)
- [x] **[BUG-P1-2]** `docker-compose.yml` `fastapi-proxy` depends on `quantum-engine: service_started` - FIXED
- [x] **[BUG-P1-5]** `lob_api.py` obsolete (C++ rewrite removed the Python bug)

### PHASE 1.5 - Module 1: C++ Network Rewrite (Tier 1 Pivot) ✅ COMPLETE

Instead of the Python Tier 3 optimizations, we are pivoting to a full C++ network layer for maximum performance.

- [x] **[M1-CPP]** Create `CMakeLists.txt` for `module1_lob` linking uWebSockets, librdkafka-c++, prometheus-cpp, and `ashuwhy/lob`.
- [x] **[M1-CPP]** Write `module1_lob/lob_server.cpp`: uWebSockets REST API + WebSocket, librdkafka consumer for `raw_orders`, librdkafka producer for `executed_trades`.
- [x] **[M1-CPP]** Update `module1_lob/Dockerfile` to compile the C++ server and run it.
- [x] **[M1-CPP]** Update `scripts/create_kafka_topics.sh` to also create `executed_trades` topic.
- [x] **[M1-CPP]** Remove all deprecated Python wrapper code (`lob_api.py`, `ring_buffer.py`, `bench_threadpool.py`).
- [x] **[M1-BENCH]** Run Siege against the new C++ endpoint: `siege -c 200 -t 30S -f module1_lob/urls.txt`; target > 100,000 QPS. Record in `benchmark_runs`.

---

## PHASE 2 - Module 2: TimescaleDB Analytics Engine

**Owner:** Member 2
**Goal:** Consume live trades from LOB via Kafka → persist to TimescaleDB → SQL indicators → REST API

### File: `module2_timescale/Dockerfile`

- [x] Placeholder exists (`FROM python:3.12-slim`)
- [x] **[M2-DOCKER]** Replace placeholder: install `libpq-dev librdkafka-dev`, copy `requirements.txt`, `pip install`, copy module, `CMD uvicorn module2_timescale.analytics_api:app --host 0.0.0.0 --port 8002`
- [x] **[M2-DOCKER]** Add `module2_timescale` service to `docker-compose.yml` with `port 8002:8002` and `depends_on: [kafka, postgres]`

### File: `module2_timescale/kafka_consumer.py`

- [x] **[M2-KAFKA]** `confluent_kafka.Consumer` on `executed_trades` topic, `group_id = 'timescale_ingestor'`
- [x] **[M2-KAFKA]** Batch 1,000 records OR 100ms timeout → `psycopg3` binary `COPY raw_ticks (ts, symbol, price, volume, side, order_id, trade_id) FROM STDIN`
- [x] **[M2-KAFKA]** Malformed JSON messages: log warning, skip record, do NOT crash consumer loop
- [x] **[M2-KAFKA]** On startup: verify `raw_ticks` hypertable exists, log current chunk count
- [x] **[M2-KAFKA]** Prometheus counter: `timescale_rows_inserted_total`
- [x] **[M2-KAFKA]** Acceptance: 10,000 rows in DB within 200ms of being published to Kafka

### File: `module2_timescale/gen_ticks.py`

- [x] **[M2-GEN]** CLI args: `--rows 1000000 --symbols BTC/USD,ETH/USD --batch-size 5000`
- [x] **[M2-GEN]** GBM price series: `dS = S * (μ dt + σ dW)`, μ=0, σ=0.02, dt=1s
- [x] **[M2-GEN]** Side B/S with 50% probability; volume `Uniform(0.01, 10.0)`
- [x] **[M2-GEN]** Bulk insert via `psycopg3` binary COPY; target ≥ 500k rows/min
- [x] **[M2-GEN]** Acceptance: `SELECT count(*) FROM raw_ticks` = target row count ± 0.1%

### File: `module2_timescale/indicators.sql`

- [x] **[M2-SQL]** `fn_vwap(symbol, from, to)` → `SUM(price*volume)/SUM(volume)` from `raw_ticks`
- [x] **[M2-SQL]** `fn_sma20(symbol, at)` → `AVG(close)` of last 20 rows from `ohlcv_1m`
- [x] **[M2-SQL]** `fn_bollinger(symbol, at)` → returns `(sma20, upper=sma20+2σ, lower=sma20-2σ)` from `ohlcv_1m`
- [x] **[M2-SQL]** `fn_rsi14(symbol, at)` → LAG window avg_gain/avg_loss over 14 periods from `ohlcv_1m`
- [x] **[M2-SQL]** All 4 functions: `LANGUAGE sql STABLE`; verified against pandas baseline within ±0.5 tolerance

### File: `module2_timescale/analytics_api.py`

- [x] **[M2-API]** `GET /analytics/ticks?symbol=&from=&to=&limit=1000` → raw rows from `raw_ticks`
- [x] **[M2-API]** `GET /analytics/ohlcv?symbol=&interval=1m|5m|15m|1h&from=&to=` → correct CA view
- [x] **[M2-API]** `GET /analytics/indicators?symbol=&indicator=vwap|sma20|bollinger|rsi&from=&to=` → call SQL functions
- [x] **[M2-API]** `GET /analytics/health` → `{"status":"ok","row_count":<int>}`
- [x] **[M2-API]** Mount under `/analytics` in `module5_security/main.py` via `app.include_router()`

### File: `module2_timescale/bench_timescale.py`

- [x] **[M2-BENCH]** Create plain PostgreSQL table `raw_ticks_plain` (identical schema, no hypertable)
- [x] **[M2-BENCH]** Load same 1M rows into both `raw_ticks` (hypertable) and `raw_ticks_plain`
- [x] **[M2-BENCH]** Run identical OHLCV range query 10× on each; record avg + p99 latency
- [x] **[M2-BENCH]** Write summary row to `benchmark_runs`; expected: hypertable ≥ 10× faster
- [x] **[M2-BENCH]** Save `benchmark_timescale.csv` + `benchmark_timescale.png` to `module2_timescale/bench_out/`

### Verification

- [x] **[M2-VERIFY]** After 1M row load: manually `CALL refresh_continuous_aggregate('ohlcv_1m', ...)`, confirm row counts; verify `ohlcv_1h` has fewer rows than `ohlcv_1m`
- [x] **[M2-VERIFY]** Run `SELECT compress_chunk(...)` on old chunk; confirm `SELECT * FROM chunk_compression_stats('raw_ticks')` shows `is_compressed = true` - screenshot for report

---

## PHASE 3 - Module 3: Graph + Bellman-Ford *(Primary Production Arbitrage Algorithm)*

**Owner:** Member 3
**Goal:** Build live FX exchange rate graph in Apache AGE; run Bellman-Ford every 500ms as the PRIMARY arbitrage detector; expose Cypher query interface; provide rate matrix endpoint for Module 4

> **Architecture decision (ADR March 9 2026):** Bellman-Ford is the production algorithm. It runs continuously, finds all profitable cycles deterministically in < 5ms, and saves `method='CLASSICAL'` signals. Quantum (Phase 4) consumes `/graph/rates` for research benchmarking only.

### File: `module3_graph/graph_init.py`

- [ ] **[M3-INIT]** `MERGE` 20 `Asset` nodes via AGE Cypher (idempotent - safe to re-run)
  - Crypto: BTC, ETH, BNB, SOL, ADA, XRP, DOGE, AVAX, MATIC, DOT
  - Fiat: USD, EUR, GBP, JPY, AUD, CAD, CHF, INR, SGD, HKD
- [ ] **[M3-INIT]** Create directed `EXCHANGE` edges with properties `{bid, ask, spread, last_updated}`; seed from `gen_ticks.py` last-price or Binance API
- [ ] **[M3-INIT]** Acceptance: node count ≥ 20; edge count ≥ 50; script is idempotent

### File: `module3_graph/edge_weight_updater.py`

- [ ] **[M3-EDGE]** asyncio loop every 500ms: `GET http://lob-engine:8001/lob/depth/{symbol}` for each active pair
- [ ] **[M3-EDGE]** Extract `best_bid = depth["bids"][0][0]`, `best_ask = depth["asks"][0][0]`
- [ ] **[M3-EDGE]** Cypher `MATCH (a)-[r:EXCHANGE]->(b) SET r.bid=$bid, r.ask=$ask, r.last_updated=timestamp()`
- [ ] **[M3-EDGE]** LOB unavailable: log warning, skip symbol, keep asyncio loop alive - do NOT crash
- [ ] **[M3-EDGE]** Prometheus gauge: `graph_edge_update_lag_ms`
- [ ] **[M3-EDGE]** Acceptance: edge `bid` value changes in AGE within 600ms of LOB price change

### File: `module3_graph/bellman_ford.py` ← PRIMARY ARBITRAGE ENGINE

- [ ] **[M3-BF]** `build_rate_matrix(conn) -> dict[tuple, float]`: query all `EXCHANGE` edges from AGE graph
- [ ] **[M3-BF]** `bellman_ford_arbitrage(rates_matrix, nodes) -> list | None`: weight `w = -log(rate)`; N-1 relaxation passes; Nth pass detects negative cycle
- [ ] **[M3-BF]** `extract_cycle(predecessor, start) -> list[str]`: walk predecessor map to extract full cycle path
- [ ] **[M3-BF]** `benchmark_bellman_ford(n_nodes, n_trials) -> dict`: returns timing stats for Module 4 comparison chart
- [ ] **[M3-BF]** Unit test: known 3-node profitable cycle detected; unprofitable graph returns `None`
- [ ] **[M3-BF]** Acceptance: runs in < 5ms at N=20 nodes

### File: `module3_graph/graph_queries.py`

- [ ] **[M3-QUERY]** `find_3hop_arbitrage_cycles(from_symbol)` - all profitable directed 3-hop cycles
- [ ] **[M3-QUERY]** `find_shortest_path(from_sym, to_sym)` - most profitable exchange route
- [ ] **[M3-QUERY]** `find_high_spread_edges(threshold)` - edges where `spread > threshold`
- [ ] **[M3-QUERY]** `crypto_subgraph()` - subgraph of crypto-only Asset nodes

### File: `module3_graph/graph_api.py`

- [ ] **[M3-API]** `GET /graph/nodes` → list all Asset vertices
- [ ] **[M3-API]** `GET /graph/edges` → list EXCHANGE edges with current bid/ask
- [ ] **[M3-API]** `GET /graph/paths?from_symbol=USD` → run `find_3hop_cycles`
- [ ] **[M3-API]** `GET /graph/rates` → N×N adjacency matrix JSON - **consumed by Module 4 `quantum_service.py`**
- [ ] **[M3-API]** `GET /graph/health` → node count + last edge update timestamp
- [ ] **[M3-API]** Mount under `/graph` in `module5_security/main.py`

---

## PHASE 4 - Module 4: Quantum Engine *(Research Benchmark vs Bellman-Ford)*

**Owner:** Member 4
**Goal:** Implement Grover's Algorithm as a research benchmark. Run BOTH Bellman-Ford and Grover on the same input. Save both results to `arbitrage_signals`. Generate the scaling comparison chart.

> **Architecture decision (ADR March 9 2026):** Quantum does NOT replace Bellman-Ford. It runs on the same rate matrix from `/graph/rates`, its wall-clock time is recorded alongside Bellman-Ford's, and the report shows the O(√N) vs O(N) complexity proof. AerSimulator is slower than Bellman-Ford - this is expected and documented.

### File: `module4_quantum/Dockerfile`

- [ ] **[M4-DOCKER]** Replace placeholder: `FROM python:3.12-slim`, install `numpy qiskit qiskit-aer psycopg[binary]`, copy module, `CMD uvicorn module4_quantum.quantum_api:app --host 0.0.0.0 --port 8004`
- [ ] **[M4-DOCKER]** **IMPLEMENT `/health` FIRST** - unblocks `fastapi-proxy` startup (BUG-P1-2 fix depends on this)

### File: `module4_quantum/grover_oracle.py`

- [ ] **[M4-ORC]** `enumerate_cycles(nodes, k=3) -> list[list]`: all P(N,3) directed 3-cycles
- [ ] **[M4-ORC]** `is_profitable(cycle, rates) -> bool`: `prod(rates[a,b] * rates[b,c] * rates[c,a]) > 1.0`
- [ ] **[M4-ORC]** `build_oracle(profitable_states, n_qubits) -> QuantumCircuit`: phase-flip oracle with MCX gates
- [ ] **[M4-ORC]** `build_diffuser(n_qubits) -> QuantumCircuit`: Grover diffusion operator `2|s><s| - I`; verify unitary = `2|s><s| - I` for N=2

### File: `module4_quantum/run_grover.py`

- [ ] **[M4-RUN]** `run_grover(rates_matrix, nodes, shots=1024) -> dict`:
  - Enumerate all 3-cycles; identify profitable states
  - Encode as `n_qubits = ceil(log2(len(cycles)))` qubit register
  - Apply H gates (uniform superposition)
  - Apply Oracle + Diffuser for `floor(π/4 × √N)` iterations
  - `AerSimulator().run(qc, shots=1024).result().get_counts()`
  - Decode top measurement → cycle → compute profit pct
- [ ] **[M4-RUN]** Return: `{path, profit_pct, circuit_depth, grover_iterations, quantum_ms, n_qubits, n_cycles}`
- [ ] **[M4-RUN]** Test N=8: correct cycle returned in > 50% of 10 independent runs; runs in < 5s
- [ ] **[M4-RUN]** Test N=16: correct cycle detected in < 30s
- [ ] **[M4-RUN]** Test N=32 (`statevector_simulator`): runs in < 120s or reports memory constraint with GB measurement
- [ ] **[M4-RUN]** Test N=64: runs or reports exact memory constraint - document result
- [ ] **[M4-RUN]** Cap at N=64 maximum (AerSimulator RAM limit)

### File: `module4_quantum/quantum_service.py`

- [ ] **[M4-SVC]** Background loop every 10 seconds:
  1. `GET /graph/rates` → fetch live N×N rate matrix from Module 3
  2. Run `bellman_ford_arbitrage()` → record `classical_ms`
  3. Run `run_grover()` → record `quantum_ms`
  4. INSERT both into `arbitrage_signals` with correct `method` field
- [ ] **[M4-SVC]** Prometheus histograms: `quantum_grover_ms`, `quantum_bellman_ford_ms`

### File: `module4_quantum/benchmark_quantum.py`

- [ ] **[M4-BENCH]** N ∈ {4, 8, 12, 16, 20, 24, 28, 32} nodes
- [ ] **[M4-BENCH]** 10 trials each: Bellman-Ford + Grover on synthetic random rate matrix
- [ ] **[M4-BENCH]** Record: `n_nodes, bellman_ford_ms_avg, bellman_ford_ms_p99, grover_ms_avg, grover_ms_p99, n_qubits, circuit_depth, grover_iterations`
- [ ] **[M4-BENCH]** Save `benchmark_quantum.csv` → `module4_quantum/bench_out/`
- [ ] **[M4-BENCH]** Generate `benchmark_quantum.png`: dual line chart (BF vs Grover), log-scale Y axis, O(N) and O(√N) theoretical reference curves
- [ ] **[M4-BENCH]** Write summary row to `benchmark_runs` table

### File: `module4_quantum/quantum_api.py`

- [ ] **[M4-API]** `GET /health` → `{"status":"ok"}` - **implement before anything else**
- [ ] **[M4-API]** `POST /quantum/run-grover` body `{graph_size_n, method}` → on-demand run
- [ ] **[M4-API]** `GET /quantum/signals?limit=50&method=QUANTUM|CLASSICAL|ALL` → query `arbitrage_signals`
- [ ] **[M4-API]** `GET /quantum/benchmark` → latest `benchmark_quantum.csv` rows as JSON
- [ ] **[M4-API]** `GET /metrics` → Prometheus exposition

---

## PHASE 5 - Module 5: Security Proxy + Observability

**Owner:** Member 5

### File: `module5_security/Dockerfile`

- [ ] **[M5-DOCKER]** Replace placeholder: `FROM python:3.12-slim`, install `libpq-dev`, copy `requirements.txt`, `pip install`, copy module, `CMD uvicorn module5_security.main:app --host 0.0.0.0 --port 8000`

### File: `module5_security/rate_limiter.py`

- [ ] **[M5-RL]** Redis sliding window: `INCR rl:{ip}` + `EXPIRE 1s`; block at count > 1,000 → HTTP 429
- [ ] **[M5-RL]** Redis unavailable: fallback to in-process `threading.Semaphore` token bucket - degraded but not crashed
- [ ] **[M5-RL]** On block: `INSERT security_events (client_ip, event_type='RATE_LIMIT', endpoint, raw_payload)`
- [ ] **[M5-RL]** Acceptance: 1,001st request in 1s → HTTP 429; confirmed `security_events` row in DB

### File: `module5_security/sql_firewall.py`

- [ ] **[M5-SQL]** `sqlglot.parse(payload)` → walk AST for DDL node types: `Drop`, `Truncate`, `Create`, `AlterTable`
- [ ] **[M5-SQL]** String scan: `DROP | TRUNCATE | UNION SELECT | -- | /* | xp_ | EXEC | INSERT INTO information_schema`
- [ ] **[M5-SQL]** On detection: HTTP 403 + `INSERT security_events (event_type='SQL_INJECTION')`
- [ ] **[M5-SQL]** Acceptance: all 10 OWASP Top-10 SQL payloads → HTTP 403; `SELECT count(*) FROM security_events WHERE event_type='SQL_INJECTION'` = 10

### File: `module5_security/main.py`

- [ ] **[M5-MAIN]** FastAPI app; middleware order: rate limiter first, then SQL firewall
- [ ] **[M5-MAIN]** Routes: `/lob/*` → `hqt-lob:8001` (httpx reverse proxy); `/analytics/*` → analytics_api router; `/graph/*` → graph_api router; `/quantum/*` → `quantum-engine:8004` (httpx reverse proxy)
- [ ] **[M5-MAIN]** `GET /health` → check all upstream services; return JSON status dict
- [ ] **[M5-MAIN]** `GET /admin/security-events?event_type=&from=&to=&limit=` → query `security_events`
- [ ] **[M5-MAIN]** `GET /admin/benchmark-runs` → query `benchmark_runs`
- [ ] **[M5-MAIN]** `GET /metrics` → `prometheus_client.generate_latest()`

### File: `module5_security/prometheus_metrics.py`

- [ ] **[M5-PROM]** Define all 7 metrics: `lob_orders_total`, `lob_trades_total`, `lob_order_latency_ms`, `lob_active_orders`, `security_sql_injections_total`, `security_rate_limit_total`, `quantum_arbitrage_signals_total`
- [ ] **[M5-PROM]** Acceptance: after 10 orders, `lob_orders_total{symbol="BTC-USD"}` = 10 in `GET /metrics`

### File: `grafana_provisioning/dashboards/hqt_main.json`

- [ ] **[M5-GRAF]** Panel 1 - Candlestick: `ohlcv_1m` for BTC/USD
- [ ] **[M5-GRAF]** Panel 2 - Depth Heatmap: LOB `/lob/depth/BTC%2FUSD` live refresh
- [ ] **[M5-GRAF]** Panel 3 - Volume bars: `SUM(volume)` from `ohlcv_1m` last 1h
- [ ] **[M5-GRAF]** Panel 4 - Arbitrage Signals table: last 20 rows from `arbitrage_signals`, both `CLASSICAL` and `QUANTUM` methods, colour-coded by `method` column
- [ ] **[M5-GRAF]** Panel 5 - QPS + p99: `rate(lob_orders_total[1m])` + `histogram_quantile(0.99, rate(lob_order_latency_ms_bucket[1m]))`
- [ ] **[M5-GRAF]** Create `grafana_provisioning/dashboards/dashboards.yml` provisioning config → dashboard appears at Grafana start without manual import

### Siege DDoS

- [ ] **[M5-SIEGE]** Run `siege -c 1000 -t 60S --log=report/siege_ddos_results.txt --content-type "application/json" -f module1_lob/urls.txt`
- [ ] **[M5-SIEGE]** Acceptance: Grafana Panel 5 shows QPS spike + recovery; `security_events` gains RATE_LIMIT rows; LOB `/health` responds within 2s after siege ends

---

## PHASE 6 - Tests, Report & Demo

### Unit Tests

- [ ] **[TEST-M1]** `tests/test_lob.py`: place crossing BUY+SELL orders → assert trade fired → assert `raw_ticks` row inserted; edge cases: zero-qty, cancel nonexistent, cross-spread - *all pass via `pytest` + `httpx.AsyncClient`*
- [ ] **[TEST-M2]** `tests/test_timescale.py`: insert 10k ticks → assert `ohlcv_1m` refreshes → assert VWAP correct to 4 decimal places vs pandas baseline
- [ ] **[TEST-M3]** `tests/test_graph.py`: seed 4-node rate matrix with known profitable cycle → assert Bellman-Ford returns correct path + profit_pct; unprofitable graph → returns None
- [ ] **[TEST-M4]** `tests/test_quantum.py`: same 4-node matrix → assert Grover returns same path as Bellman-Ford within 3 independent runs; `enumerate_cycles` count = P(N,3) for N=4 and N=8
- [ ] **[TEST-M5]** `tests/test_security.py`: 10 OWASP SQL injection payloads → HTTP 403 + `security_events` row; rate limit +1 → HTTP 429; valid request → 200
- [ ] **[TEST-E2E]** `tests/test_e2e.py`: `docker compose up -d` → POST 100 orders → assert ticks in `raw_ticks` + AGE edge updated + arbitrage signal in `arbitrage_signals`
- [ ] **[TEST-CFG]** Add `pyproject.toml` `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"` → `pytest` from root discovers all tests

### Report

- [ ] **[REPORT]** `report/final_report.md` ≥ 10 pages / ≥ 3,000 words - sections: Architecture, LOB benchmarks, TimescaleDB vs plain PG comparison, Bellman-Ford live arbitrage results, Quantum vs Classical complexity analysis, Security demo
- [ ] **[REPORT]** Include `benchmark_quantum.png` - BF flat line vs Grover exponential; narrative: "Bellman-Ford is the production algorithm (deterministic, O(N·E), < 5ms). Grover provides a theoretical O(√N) query complexity advantage proven only on real quantum hardware; AerSimulator computes the full state vector classically, producing exponential overhead that inverts this advantage."
- [ ] **[REPORT]** Include `bench_out/latency_histogram.png` from `ashuwhy/lob` - embed directly from submodule
- [ ] **[REPORT]** Include Grafana screenshots for all 5 panels; include Siege log summary
- [ ] **[REPORT]** `pandoc --pdf-engine=xelatex report/final_report.md -o report/final_report.pdf` → PDF renders all figures; page count ≥ 10
- [ ] **[DEMO]** `report/demo_script.md` - ordered 20-minute walkthrough with exact `curl` commands and expected output for each module
- [ ] **[DEMO]** Screenshot Grafana Panel 5 during Siege load; save as `report/grafana_siege_screenshot.png`

### Code Freeze

- [ ] **[RELEASE]** `pre-commit run --all-files` → zero linting errors
- [ ] **[RELEASE]** Full `pytest` → all tests pass; runtime < 5 minutes
- [ ] **[RELEASE]** `docker compose down -v && docker compose up -d --build` from clean state → system fully up in < 10 minutes
- [ ] **[RELEASE]** Merge `dev → main`; tag `v1.0.0`; verify `.gitmodules` references `ashuwhy/lob`; push to GitHub; share link with instructor before **April 15, 2026**

---

## Appendix A - Final File Structure

```
hqt/
├── .env.example                            ✅
├── .gitignore                              ✅
├── .gitmodules                             ✅ (after submodule add)
├── .pre-commit-config.yaml                 ✅
├── docker-compose.yml                      ✅ + 3 bug fixes + port 8002
├── init.sql                                ✅ + 3 CA additions + 3 PK fixes
├── requirements.txt                        ✅
├── requirements-dev.txt                    ✅
├── pyproject.toml                          ← NEW (pytest config)
├── scripts/
│   └── create_kafka_topics.sh              ✅
├── docker/
│   ├── Dockerfile.postgres                 ✅
│   └── prometheus.yml                      ✅
├── docs/
│   ├── API_SPEC.md                         ✅
│   ├── ARCHITECTURE.md                     ← UPDATED (ADR added)
│   ├── DATABASE_SCHEMA.md                  ✅ (PK fixes reflected)
│   ├── MODULE_SPECS.md                     ← UPDATED (BF primary, Quantum benchmark)
│   ├── TASK_LIST.md                        ← UPDATED (this file)
│   └── PRD.md                             ✅
├── module1_lob/
│   ├── engine/                             ✅ (git submodule: ashuwhy/lob)
│   ├── Dockerfile                          ← UPDATED (C++ compiler)
│   ├── CMakeLists.txt                      ← NEW
│   ├── lob_server.cpp                      ← NEW
│   ├── urls.txt                            ✅ + Siege format fixed
│   └── siege.conf                          ← NEW
├── module2_timescale/
│   ├── Dockerfile                          ← IN PROGRESS (placeholder → real)
│   ├── kafka_consumer.py                   ← NEW
│   ├── gen_ticks.py                        ← NEW
│   ├── indicators.sql                      ← NEW
│   ├── analytics_api.py                    ← NEW
│   └── bench_timescale.py                  ← NEW
├── module3_graph/
│   ├── graph_init.py                       ← NEW
│   ├── edge_weight_updater.py              ← NEW
│   ├── bellman_ford.py                     ← NEW  ★ PRIMARY ARBITRAGE
│   ├── graph_queries.py                    ← NEW
│   └── graph_api.py                        ← NEW
├── module4_quantum/
│   ├── Dockerfile                          ← IN PROGRESS (placeholder → real + /health)
│   ├── grover_oracle.py                    ← NEW
│   ├── run_grover.py                       ← NEW
│   ├── quantum_service.py                  ← NEW  ★ RUNS BOTH ALGORITHMS
│   ├── quantum_api.py                      ← NEW  (GET /health FIRST)
│   └── benchmark_quantum.py               ← NEW
├── module5_security/
│   ├── Dockerfile                          ← IN PROGRESS
│   ├── main.py                             ← NEW
│   ├── sql_firewall.py                     ← NEW
│   ├── rate_limiter.py                     ← NEW
│   ├── prometheus_metrics.py               ← NEW
│   └── grafana_provisioning/
│       ├── datasources/datasources.yml     ✅
│       └── dashboards/
│           ├── dashboards.yml              ← NEW
│           └── hqt_main.json              ← NEW (Panel 4: both CLASSICAL + QUANTUM)
├── tests/
│   ├── test_lob.py                         ← NEW
│   ├── test_timescale.py                   ← NEW
│   ├── test_graph.py                       ← NEW
│   ├── test_quantum.py                     ← NEW
│   ├── test_security.py                    ← NEW
│   └── test_e2e.py                         ← NEW
└── report/
    ├── final_report.md                     ← NEW
    ├── final_report.pdf                    ← NEW
    ├── demo_script.md                      ← NEW
    ├── siege_ddos_results.txt              ← NEW (after Siege run)
    └── grafana_siege_screenshot.png        ← NEW
```

---

## Appendix B - Critical Path

```
Fix BUG-P0-1/P0-2/P0-3 (docker-compose env vars)
Fix BUG-P1-1 (ring_buffer[seq])
Fix BUG-P1-2 (quantum service_started) ← unblocked by M4 /health stub
Fix BUG-P1-5 (asyncio loop storage)
        │
        ▼
Phase 2 (TimescaleDB pipeline)
        │
        ▼
Phase 3 (Bellman-Ford LIVE - primary arbitrage running every 500ms)
        │
        ├─► Phase 4 (Grover benchmark consumes /graph/rates - every 10s)
        │
        ▼
Phase 5 (Security proxy mounts all 4 modules)
        │
        ▼
Phase 6 (Tests + benchmark_quantum.png + report + demo)
        │
        ▼
    April 15, 2026 - tag v1.0.0, submit
```

---

## Appendix C - Week-by-Week Schedule

| Week | Dates | Milestone |
|------|-------|-----------|
| 1 | Mar 8–14 | All Phase 0 + Phase 1 bugs fixed; M1 bench verified |
| 2 | Mar 15–21 | Phase 2 complete: Kafka consumer live, 1M rows, analytics API up |
| 3 | Mar 22–28 | Phase 3 complete: Bellman-Ford running every 500ms, signals in DB; intermediate demo |
| 4 | Mar 29 – Apr 7 | Phase 4 complete: Grover benchmark chart generated; Phase 5 complete; Siege run done |
| 5 | Apr 8–14 | Phase 6: all tests passing; report ≥ 10 pages; demo dry run completed |
| **Submit** | **Apr 15** | **Tag v1.0.0 · push to GitHub · share link** |
