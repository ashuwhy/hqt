# Granular Task List — Final

## Hybrid Trading Database System

> **90 implementation tasks | 39 files to create | 2 existing files to fix | 10 files confirmed done**
> **Deadline: April 15, 2026**

---

## Status Key

- `[x]` Done (infrastructure layer)
- `[ ]` Not started
- `⚠️` Bug in existing file — fix required before building on top

---

## Already Complete ✅

| File | Status |
|------|--------|
| `docker-compose.yml` (base 6 services) | ✅ Done — needs 7 additions |
| `init.sql` (base tables + ohlcv_1m) | ✅ Done — needs 3 CA additions + 3 PK fixes |
| `docker/Dockerfile.postgres` | ✅ Done |
| `docker/prometheus.yml` | ✅ Done — targets valid once services added |
| `requirements.txt` | ✅ Done |
| `requirements-dev.txt` | ✅ Done |
| `.gitignore` | ✅ Done |
| `.pre-commit-config.yaml` | ✅ Done |
| `module5_security/grafana_provisioning/datasources/datasources.yml` | ✅ Done |
| All `docs/` files | ✅ Done (this update replaces them) |

---

## PHASE 0 — Infrastructure Fixes *(Do These First — Everything Depends On Them)*

### Fix `init.sql` ⚠️

- [x] **[INFRA-FIX]** Add `ohlcv_5m` continuous aggregate: `time_bucket('5 minutes', ts)` with `schedule_interval => INTERVAL '5 minutes'` refresh policy inside idempotent `DO $$ IF NOT EXISTS $$` block — *`SELECT count(*) FROM ohlcv_5m` returns data within 5 min of tick insertion*
- [x] **[INFRA-FIX]** Add `ohlcv_15m` continuous aggregate — same pattern — *`SELECT count(*) FROM ohlcv_15m` returns data within 15 min*
- [x] **[INFRA-FIX]** Add `ohlcv_1h` continuous aggregate — same pattern — *`SELECT count(*) FROM ohlcv_1h` returns data within 1 hr*
- [x] **[INFRA-FIX]** Fix `trades` composite PK `(trade_id, ts)` → `PRIMARY KEY (trade_id)` — *`\d trades` shows single-column PK; FK refs from other tables succeed*
- [x] **[INFRA-FIX]** Fix `arbitrage_signals` composite PK `(signal_id, ts)` → `PRIMARY KEY (signal_id)` — *`\d arbitrage_signals` shows single-column PK*
- [x] **[INFRA-FIX]** Fix `security_events` composite PK `(event_id, ts)` → `PRIMARY KEY (event_id)` — *`\d security_events` shows single-column PK*

### Fix `docker-compose.yml` ⚠️ — Add 7 Missing Services

- [x] **[INFRA-FIX]** Add `lob-engine` service: `build: module1_lob/`, port `8001:8001`, `depends_on: [kafka, postgres]`, healthcheck `GET /health` — *`docker compose ps` shows `hqt-lob` healthy*
- [x] **[INFRA-FIX]** Add `data-ingestor` service: `build: module2_timescale/`, `depends_on: [kafka, postgres]` — *Container starts and logs "Kafka consumer running"*
- [x] **[INFRA-FIX]** Add `quantum-engine` service: `build: module4_quantum/`, port `8004:8004`, `depends_on: [postgres]` — *`GET /health` returns 200*
- [x] **[INFRA-FIX]** Add `fastapi-proxy` service: `build: module5_security/`, port `8000:8000`, `depends_on: [postgres, redis, lob-engine, quantum-engine]` — *`curl localhost:8000/health` returns all-OK JSON*
- [x] **[INFRA-FIX]** Add `postgres-exporter` (`prometheuscommunity/postgres-exporter`), env `DATA_SOURCE_NAME=postgresql://hqt:hqt_secret@postgres:5432/hqt`, port `9187:9187` — *Prometheus target shows UP*
- [x] **[INFRA-FIX]** Add `redis-exporter` (`oliver006/redis_exporter`), port `9121:9121` — *Prometheus target shows UP*
- [x] **[INFRA-FIX]** Add `node-exporter` (`prom/node-exporter`), port `9100:9100`, `/proc` + `/sys` read-only bind mounts — *Prometheus target shows UP*

### Missing Config Files

- [x] **[INFRA]** Create `.env.example` with: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `REDIS_URL`, `KAFKA_BOOTSTRAP_SERVERS`, `LOB_ENGINE_URL`, `QUANTUM_ENGINE_URL`, `BINANCE_WS_URL`, `ALPHA_VANTAGE_API_KEY` — *System boots from a `.env.example` copy without missing-variable errors*
- [x] **[INFRA]** Create `scripts/create_kafka_topics.sh`: `kafka-topics.sh --create --topic raw_orders --partitions 4 --replication-factor 1 --if-not-exists` — *Idempotent; topic exists after any run*
- [x] **[INFRA]** Add `kafka-setup` one-shot service to `docker-compose.yml`: runs `create_kafka_topics.sh` after `kafka` passes healthcheck, then exits 0 — *No manual topic creation needed on fresh `docker compose up`*

---

## PHASE 1 — Module 1: LOB Engine *(Wrap the C++ Engine — Do Not Rewrite)*

> ⚡ The `.gemini-tmp-limit-order-book/` C++ engine is complete and benchmarked. The task is to integrate it via Git Submodule and wrap with FastAPI. Delete the loose temp folder after adding the submodule.

### Repo Cleanup & Engine Integration

- [x] **[CLEANUP]** Delete `.gemini-tmp-limit-order-book/` directory from project root — *`git status` shows it removed; history preserved via submodule*
- [x] **[M1-PORT]** Run `git submodule add https://github.com/ashuwhy/lob module1_lob/engine` — *`.gitmodules` file created; `module1_lob/engine/` populated*
- [x] **[M1]** Create `module1_lob/Dockerfile`: `FROM python:3.12-slim`, install `build-essential cmake python3-dev pybind11-dev`, clone submodule, `cmake --build build/`, `pip install -e .` — *`python -c "from olob import OrderBook"` passes inside container*
- [x] **[M1]** Verify `olob._bindings` C++ extension compiles against Python 3.12 — *`dir(olob._bindings)` lists `OrderBook`, `Side`, `OrderType`; `bench_out/latencies.csv` re-runnable*

### FastAPI Wrapper (`lob_api.py`)

- [x] **[M1]** Create `module1_lob/lob_api.py`: maintain one `OrderBook` instance per symbol in a `dict`; `POST /lob/order` calls `book.add_limit_order()` or `book.add_market_order()`, persists to `orders` table via `psycopg3` — *Returns HTTP 201 with UUID `order_id`*
- [x] **[M1]** Implement `DELETE /lob/order/{order_id}`: calls `book.cancel_order()`, sets `orders.status = 'CANCELLED'` — *Cancelled status confirmed in DB; GET depth no longer shows that order*
- [x] **[M1]** Implement `PATCH /lob/order/{order_id}`: atomic cancel + re-place at new price/qty — *Order appears at new price level in depth snapshot*
- [x] **[M1]** Implement `GET /lob/depth/{symbol}`: calls `book.get_depth(levels=10)`, returns top-10 bids/asks — *Response schema matches `docs/API_SPEC.md`*
- [x] **[M1]** Implement WebSocket `ws://localhost:8001/lob/stream/{symbol}`: async push `DEPTH_UPDATE` message on every match event — *WS client receives update within 50ms of a trade*
- [x] **[M1]** Implement `GET /lob/health`: returns active symbols list + status — *Used by docker-compose healthcheck; returns 200*
- [x] **[M1]** Implement `GET /lob/metrics`: Prometheus exposition — `lob_orders_total`, `lob_trades_total`, `lob_order_latency_ms` Histogram, `lob_active_orders` Gauge — *All metrics visible after 10 orders placed*

### LMAX-Pattern Persistence Thread

- [x] **[M1]** Create `module1_lob/ring_buffer.py`: fixed `2^20`-slot array, sequence-number producer/consumer (no mutex, memory barriers only) — *Micro-benchmark: > 1M messages/sec through ring buffer alone*
- [x] **[M1]** Implement `PersistenceThread`: reads TradeEvents from ring buffer, batches ≤ 1,000 or 100ms timeout, bulk `psycopg3.copy()` into `raw_ticks` + `trades` — *1,000 synthetic trades via API → all rows in DB within 300ms*

### Kafka Inbound Thread

- [x] **[M1]** Implement `InboundThread`: `confluent_kafka.Consumer` on `raw_orders` → deserialize JSON → write OrderEvent to ring buffer → ring buffer feeds MatchingThread — *100 Kafka messages → 100 orders processed*

### Benchmarking

- [x] **[M1]** Write `module1_lob/bench_threadpool.py`: `ThreadPoolExecutor(200)`, fires `POST /lob/order` for 30s, computes QPS + p50/p99/p999, writes row to `benchmark_runs` table — *Script produces clean CSV output; no errors*
- [x] **[M1]** Write `module1_lob/urls.txt` for Siege with 5 representative order payloads — *`siege -c 200 -t 10S -f module1_lob/urls.txt` runs without connection errors*

---

## PHASE 2 — Module 2: TimescaleDB Temporal Analytics

- [ ] **[M2]** Create `module2_timescale/kafka_consumer.py`: `confluent_kafka.Consumer` on `raw_orders`, batch 1,000 records or 100ms, `psycopg3.copy()` binary COPY into `raw_ticks` — *10,000 rows in DB within 200ms; consumer loop doesn't crash on malformed JSON (bad messages logged and skipped)*
- [ ] **[M2]** Create `module2_timescale/gen_ticks.py`: GBM price series, `faker` UUIDs, 1M rows for 10 symbols; CLI args `--rows`, `--symbols`, `--batch-size` — *Completes in < 5 minutes; `SELECT count(*) FROM raw_ticks` ≥ 1,000,000*
- [ ] **[M2]** Verify all 4 OHLCV continuous aggregates after data load: `CALL refresh_continuous_aggregate(...)`, confirm row counts, verify `ohlcv_1h` has fewer rows than `ohlcv_1m` — *Correct hierarchical aggregation confirmed in psql*
- [ ] **[M2]** Create `module2_timescale/indicators.sql`: SQL functions `fn_vwap`, `fn_sma20`, `fn_bollinger`, `fn_rsi14` — *Each verified against pandas baseline within ±0.5 tolerance using test dataset*
- [ ] **[M2]** Create `module2_timescale/analytics_api.py`: FastAPI router with `GET /analytics/ticks`, `GET /analytics/ohlcv` (interval `1m|5m|15m|1h`), `GET /analytics/indicators` — *All endpoints return valid paginated JSON; mounted at `/analytics` in `main.py`*
- [ ] **[M2]** Create `module2_timescale/bench_timescale.py`: plain table + hypertable, same 1M rows, same OHLCV query 10× each, write both result rows to `benchmark_runs` — *Hypertable ≥ 10× faster documented; CSV printed to stdout*
- [ ] **[M2]** Verify compression: manually compress old chunks with `SELECT compress_chunk(...)`, confirm `SELECT * FROM chunk_compression_stats('raw_ticks')` shows `is_compressed = true` — *Screenshot saved for report*
- [ ] **[M2]** Create `module2_timescale/Dockerfile` — *`data-ingestor` container starts and logs "Kafka consumer running"*

---

## PHASE 3 — Module 3: Apache AGE Graph Layer

- [ ] **[M3]** Create `module3_graph/graph_init.py`: `SET search_path = ag_catalog, public`; `MERGE` 20+ Asset nodes (10 crypto + 10 fiat); create `EXCHANGE` directed edges with synthetic bid/ask; script is idempotent — *`MATCH (n:Asset) RETURN count(n)` ≥ 20; `MATCH ()-[r:EXCHANGE]->() RETURN count(r)` ≥ 50*
- [ ] **[M3]** Create `module3_graph/edge_weight_updater.py`: asyncio loop every 500ms; fetch best-bid from `GET /lob/depth/{symbol}`; update AGE edge `bid` via parameterized Cypher — *Within 600ms of LOB price change, AGE edge reflects new value*
- [ ] **[M3]** Handle LOB unavailability in updater: log warning, skip symbol, keep asyncio loop alive — *Kill LOB container → worker logs warnings every 500ms, does not crash*
- [ ] **[M3]** Create `module3_graph/bellman_ford.py`: `bellman_ford_arbitrage(rates_matrix, nodes)` using `-log(rate)` edge weights; `benchmark_bellman_ford(n_nodes, n_trials)` returning timing stats for comparison with Grover — *Unit test: known 3-node profitable cycle detected; unprofitable graph returns None*
- [ ] **[M3]** Create `module3_graph/graph_queries.py`: 4 Cypher query functions: `find_3hop_arbitrage_cycles`, `find_shortest_path`, `find_high_spread_edges`, `crypto_subgraph` — *Each returns valid Python list/dict verified on synthetic data*
- [ ] **[M3]** Create `module3_graph/graph_api.py`: `GET /graph/nodes`, `GET /graph/edges`, `GET /graph/paths?from_symbol=` — *`/graph/paths?from_symbol=USD` returns cycle array; mounted at `/graph` in `main.py`*

---

## PHASE 4 — Module 4: Quantum Arbitrage Engine

- [ ] **[M4]** Create `module4_quantum/grover_oracle.py`: `build_oracle(profitable_states, n_qubits)` — X-gate inversion mask + MCX Toffoli per profitable state — *`qc.draw()` shows correct gate structure for N=3 qubits*
- [ ] **[M4]** Create `module4_quantum/grover_diffuser.py`: `build_diffuser(n_qubits)` — H+X+MCX+X+H inversion-about-mean — *Unitary matrix matches `2|s><s| - I` for N=2*
- [ ] **[M4]** Create `module4_quantum/run_grover.py`: full pipeline `enumerate_3cycles` → `is_profitable` → oracle + diffuser → `AerSimulator` → decode top measurement — *N=8 graph with 1 known cycle: correct cycle returned in > 50% of 10 independent runs*
- [ ] **[M4]** Scale and test N=8: `n_qubits = ceil(log2(P(8,3))) = 9` — *Runs in < 5s on AerSimulator*
- [ ] **[M4]** Scale and test N=16 — *Correct cycle detected in < 30s*
- [ ] **[M4]** Scale and test N=32 using `statevector_simulator` — *Runs in < 120s or reports meaningful memory constraint with GB measurement*
- [ ] **[M4]** Scale and test N=64 — *Runs or reports exact memory constraint; result documented in report*
- [ ] **[M4]** Create `module4_quantum/quantum_service.py`: wraps `run_grover()` + `bellman_ford_arbitrage()`; INSERTs one row each to `arbitrage_signals` with `method='QUANTUM'` and `method='CLASSICAL'` — *Two rows per run confirmed in DB*
- [ ] **[M4]** Create `module4_quantum/benchmark_quantum.py`: N ∈ {8,16,32,64}, 10 trials per method, saves `benchmark_quantum.csv` + `quantum_scaling.png` log-log plot with O(√N) and O(N) reference lines — *Both files generated and committed to repo*
- [ ] **[M4]** Create `module4_quantum/quantum_api.py`: `POST /quantum/run-grover`, `GET /quantum/signals` — *POST returns signal data in < 60s; mounted at `/quantum` in `main.py`*
- [ ] **[M4]** Create `module4_quantum/Dockerfile` — *`quantum-engine` container starts healthy*

---

## PHASE 5 — Module 5: Security Proxy & Observability

- [ ] **[M5]** Create `module5_security/main.py`: FastAPI app mounting all 4 module routers (`/lob`, `/analytics`, `/graph`, `/quantum`); `GET /health` checks all downstream services; `GET /metrics` — *All module endpoints reachable via port 8000; health returns all-OK JSON*
- [ ] **[M5]** Create `module5_security/sql_firewall.py`: `SQLInjectionMiddleware` with `sqlglot` AST check + banned-pattern string scan; returns HTTP 403 + logs to `security_events` — *All 10 OWASP Top-10 SQL injection payloads blocked; `DROP TABLE` in AST triggers 403*
- [ ] **[M5]** Verify firewall logging: send 5 injection payloads → `SELECT count(*) FROM security_events WHERE event_type='SQL_INJECTION'` = 5 exactly
- [ ] **[M5]** Create `module5_security/rate_limiter.py`: `RateLimitMiddleware` — Redis sliding window 1,000 req/sec/IP; HTTP 429 + log to `security_events`; fallback to in-process token bucket if Redis is down — *1,001st request in 1s → 429; Redis killed → degraded but not crashed*
- [ ] **[M5]** Create `module5_security/prometheus_metrics.py`: all 7 counters/histograms/gauges from `MODULE_SPECS.md`; instrument LOB endpoints — *After 10 orders, `lob_orders_total{symbol="BTC-USD"}` = 10 in `GET /metrics`*
- [ ] **[M5]** Create `module5_security/grafana_provisioning/dashboards/hqt_main.json`: 5 panels (candlestick, depth heatmap, volume bars, arbitrage signals table, QPS+p99 Prometheus) — *All 5 panels load in Grafana without "No data" or errors*
- [ ] **[M5]** Create `module5_security/grafana_provisioning/dashboards/dashboards.yml`: dashboard provisioning config pointing at `hqt_main.json` — *Dashboard appears in Grafana without manual import after container start*
- [ ] **[M5]** Create `module5_security/Dockerfile` — *`fastapi-proxy` container starts healthy on port 8000*
- [ ] **[M5]** Run Siege DDoS simulation: `siege -c 1000 -t 60S -f module1_lob/urls.txt` — *Grafana shows QPS spike; `security_events` gains RATE_LIMIT rows; LOB health responds within 2s after siege stops*
- [ ] **[M5]** Save Siege output to `report/siege_ddos_results.txt` and commit — *File present in repo*

---

## PHASE 6 — Tests, Integration, Report & Demo

### Unit Tests

- [ ] **[TEST]** Create `tests/test_lob.py`: 10+ cases — place/cancel/modify/depth + edge cases (zero-qty, cancel nonexistent, cross-spread) — using `pytest` + `httpx.AsyncClient` — *All pass*
- [ ] **[TEST]** Create `tests/test_timescale.py`: VWAP/SMA20/RSI vs pandas baseline (±0.5); OHLCV pagination correctness — *All pass*
- [ ] **[TEST]** Create `tests/test_graph.py`: node/edge counts after `graph_init.py`; Bellman-Ford with known profitable + unprofitable graph; edge-weight update reflected in AGE within 600ms — *All pass*
- [ ] **[TEST]** Create `tests/test_quantum.py`: `enumerate_3cycles` count formula `P(N,3)` for N=4,8; `is_profitable` predicate for known cycles; Grover correct for N=8 toy graph — *All pass; total suite runtime < 120s*
- [ ] **[TEST]** Create `tests/test_security.py`: 10 OWASP payloads → HTTP 403; rate limit + 1 → HTTP 429; valid request → 200 — *All 15+ cases pass*
- [ ] **[TEST]** Add `pyproject.toml` with `[tool.pytest.ini_options]`: `testpaths = ["tests"]`, `asyncio_mode = "auto"` — *`pytest` from repo root discovers and runs all test files*

### End-to-End Test

- [ ] **[ALL]** Create `tests/test_e2e.py`: 5-stage pipeline test — (1) POST order → (2) tick appears in `raw_ticks` → (3) `ohlcv_1m` updates → (4) AGE edge weight changes → (5) arbitrage signal written — *All assertions pass without manual intervention*
- [ ] **[ALL]** Fix all bugs uncovered during e2e — *Re-run passes clean*

### Report & Demo

- [ ] **[ALL]** Write `report/final_report.md` (≥ 10 pages, ≥ 3,000 words): one section per module with SQL listings, circuit diagrams, benchmark tables, Grafana screenshots — *All figures referenced inline; references section included*
- [ ] **[ALL]** Convert to `report/final_report.pdf` via `pandoc --pdf-engine=xelatex` — *PDF renders all figures correctly; page count ≥ 10*
- [ ] **[ALL]** Write `report/demo_script.md`: ordered 20-minute live demo with exact `curl` commands and expected output for each of the 5 modules — *Dry run completed without surprises*

### Code Freeze & Submission

- [ ] **[ALL]** `pre-commit run --all-files` — zero linting errors
- [ ] **[ALL]** Full `pytest` — all tests pass
- [ ] **[ALL]** `docker compose down -v && docker compose up -d --build` from clean state — *System fully up in < 10 minutes*
- [ ] **[ALL]** Merge `dev → main`, tag `v1.0.0`, push to GitHub, share link with instructor before **April 15, 2026**

---

## Appendix A — Final File Structure

```
hqt/
├── .env.example                       ← NEW
├── .gitignore                         ✅ Done
├── .gitmodules                        ← NEW (after submodule add)
├── .pre-commit-config.yaml            ✅ Done
├── docker-compose.yml                 ✅ + 7 services added
├── init.sql                           ✅ + 3 CAs + 3 PK fixes
├── requirements.txt                   ✅ Done
├── requirements-dev.txt               ✅ Done
├── pyproject.toml                     ← NEW (pytest config)
├── scripts/
│   └── create_kafka_topics.sh         ← NEW
├── docker/
│   ├── Dockerfile.postgres            ✅ Done
│   └── prometheus.yml                 ✅ Done
├── docs/
│   ├── API_SPEC.md                    ✅ Done
│   ├── ARCHITECTURE.md                ← UPDATED (this PR)
│   ├── DATABASE_SCHEMA.md             ✅ Done
│   ├── MODULE_SPECS.md                ← UPDATED (this PR)
│   ├── TASK_LIST.md                   ← UPDATED (this PR)
│   └── PRD.md                        ✅ Done
├── module1_lob/
│   ├── engine/                        ← NEW (git submodule: ashuwhy/lob)
│   ├── Dockerfile                     ← NEW
│   ├── lob_api.py                     ← NEW
│   ├── ring_buffer.py                 ← NEW
│   ├── bench_threadpool.py            ← NEW
│   └── urls.txt                       ← NEW
├── module2_timescale/
│   ├── Dockerfile                     ← NEW
│   ├── kafka_consumer.py              ← NEW
│   ├── gen_ticks.py                   ← NEW
│   ├── indicators.sql                 ← NEW
│   ├── analytics_api.py               ← NEW
│   └── bench_timescale.py             ← NEW
├── module3_graph/
│   ├── graph_init.py                  ← NEW
│   ├── edge_weight_updater.py         ← NEW
│   ├── bellman_ford.py                ← NEW
│   ├── graph_queries.py               ← NEW
│   └── graph_api.py                   ← NEW
├── module4_quantum/
│   ├── Dockerfile                     ← NEW
│   ├── grover_oracle.py               ← NEW
│   ├── grover_diffuser.py             ← NEW
│   ├── run_grover.py                  ← NEW
│   ├── quantum_service.py             ← NEW
│   ├── quantum_api.py                 ← NEW
│   └── benchmark_quantum.py           ← NEW
├── module5_security/
│   ├── Dockerfile                     ← NEW
│   ├── main.py                        ← NEW
│   ├── sql_firewall.py                ← NEW
│   ├── rate_limiter.py                ← NEW
│   ├── prometheus_metrics.py          ← NEW
│   └── grafana_provisioning/
│       ├── datasources/
│       │   └── datasources.yml        ✅ Done
│       └── dashboards/
│           ├── dashboards.yml         ← NEW
│           └── hqt_main.json          ← NEW
├── tests/
│   ├── test_lob.py                    ← NEW
│   ├── test_timescale.py              ← NEW
│   ├── test_graph.py                  ← NEW
│   ├── test_quantum.py                ← NEW
│   ├── test_security.py               ← NEW
│   └── test_e2e.py                    ← NEW
└── report/
    ├── final_report.md                ← NEW
    ├── final_report.pdf               ← NEW
    ├── demo_script.md                 ← NEW
    └── siege_ddos_results.txt         ← NEW (after Siege run)
```

---

## Appendix B — Critical Path

```
PHASE 0 (infra fixes)
    └─► PHASE 1 (LOB C++ wrap + FastAPI)  ← HIGHEST LEVERAGE TASK
              └─► PHASE 2 (TimescaleDB pipeline)
              └─► PHASE 3 (Graph — needs LOB for edge updates)
                    └─► PHASE 4 (Quantum — needs Graph for rate matrix)
                          └─► PHASE 5 (Security proxy mounts all 4 modules)
                                └─► PHASE 6 (Tests + Report + Demo)
```

The `ashuwhy/lob` submodule contains a battle-tested C++ engine with nanosecond benchmarks. Wrapping it (Phase 1) unlocks all downstream modules simultaneously and will likely exceed the 100,000 orders/sec target by an order of magnitude.

---

## Appendix C — Week-by-Week Schedule

| Week | Dates | Milestone |
|------|-------|-----------|
| 1 | Mar 8–14 | Phase 0 complete; submodule added; `.env.example` done |
| 2 | Mar 15–21 | Phase 1 + Phase 2 implementation done |
| 3 | Mar 22–28 | Phase 3 + Phase 4 (N=8, N=16) done; intermediate demo |
| 4 | Mar 29 – Apr 7 | Phase 4 (N=32, N=64) + Phase 5 complete; Siege run done |
| 5 | Apr 8–14 | Phase 6 — all tests passing; report written; demo dry run |
| **Submit** | **Apr 15** | **Tag v1.0.0; push to GitHub; share link** |
