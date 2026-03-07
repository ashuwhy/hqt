# Granular Task List
## Hybrid Trading Database System – AI Agent Execution Plan

> Format: `[MODULE] [OWNER] TASK — Acceptance Criteria`

---

## PHASE 0 – Repository & Infrastructure (Week 1: Mar 8–14)

- [ ] **[INFRA]** Create GitHub repo with `main` and `dev` branches; add `.gitignore` for Python, Node, Java — *Repo exists, CI pre-commit hook runs linting*
- [ ] **[INFRA]** Write `docker-compose.yml` with services: `zookeeper`, `kafka`, `postgres` (TimescaleDB+AGE image), `redis`, `prometheus`, `grafana` — *`docker compose up` brings all services healthy*
- [ ] **[INFRA]** Write `init.sql` that: loads TimescaleDB extension, loads AGE extension, creates all tables from DATABASE_SCHEMA.md — *All tables exist after container start*
- [ ] **[INFRA]** Create Kafka topic `raw_orders` with 4 partitions, replication factor 1 — *`kafka-topics.sh --list` shows `raw_orders`*
- [ ] **[INFRA]** Set up IBM Qiskit local environment: `pip install qiskit qiskit-aer`, run sample Bell circuit — *Sample circuit executes without error*
- [ ] **[INFRA]** Configure Prometheus `prometheus.yml` to scrape FastAPI `/metrics`, postgres-exporter, redis-exporter, node-exporter — *Prometheus targets page shows all UP*
- [ ] **[INFRA]** Import Grafana provisioning YAML for datasources (TimescaleDB + Prometheus) — *Both datasources appear in Grafana UI*
- [ ] **[INFRA]** Write `requirements.txt` covering: `fastapi`, `uvicorn`, `psycopg[binary]`, `confluent-kafka`, `sqlglot`, `redis`, `qiskit`, `qiskit-aer`, `sortedcontainers`, `prometheus-client`, `faker`, `numpy`, `pytest` — *`pip install -r requirements.txt` succeeds cleanly*

---

## PHASE 1 – Core Engine Implementation (Week 2: Mar 15–21)

### Module 1 – LOB Engine

- [ ] **[M1-M1]** Implement `OrderBook` class with `SortedDict` for bid/ask trees and `deque` per price level — *Unit test: 10,000 random orders inserted in < 500ms*
- [ ] **[M1-M1]** Implement `place_order(order)` with price-time priority matching; emit `TradeEvent` on match — *Unit test: crossing bid/ask produces correct trade at correct price/qty*
- [ ] **[M1-M1]** Implement `cancel_order(order_id)` using O(1) UUID→Order lookup dict — *Unit test: cancelled order no longer appears in depth snapshot*
- [ ] **[M1-M1]** Implement `modify_order(order_id, new_price, new_qty)` as atomic cancel+replace — *Unit test: modified order appears at new price level with updated qty*
- [ ] **[M1-M1]** Implement `get_depth(symbol, levels=10)` returning top-N bids/asks — *Returns correct sorted depth in < 1ms*
- [ ] **[M1-M1]** Implement LMAX Disruptor ring buffer (`RingBuffer` class, size 2^20) with sequence-based lock-free producer/consumer — *Throughput test: > 1M messages/sec through ring buffer alone*
- [ ] **[M1-M1]** Wire three threads: `InboundThread` (Kafka consumer → ring buffer), `MatchingThread` (ring buffer → LOB), `PersistenceThread` (trade events → TimescaleDB COPY) — *End-to-end: order placed via Kafka appears in `trades` table within 200ms*
- [ ] **[M1-M1]** Wrap LOB in FastAPI: implement `POST /lob/order`, `DELETE /lob/order/{id}`, `PATCH /lob/order/{id}`, `GET /lob/depth/{symbol}` — *All endpoints return correct HTTP status codes*
- [ ] **[M1-M1]** Implement WebSocket endpoint `ws://localhost:8001/lob/stream/{symbol}` that streams depth-diff events — *WebSocket client receives update within 50ms of a trade*
- [ ] **[M1-M1]** Write Siege URL file and custom `bench_threadpool.py` benchmark script — *Script runs and outputs QPS + p99 to stdout and saves to `benchmark_runs` table*

### Module 2 – TimescaleDB Pipeline

- [ ] **[M2-M2]** Write Kafka consumer that reads from `raw_orders` topic and batch-inserts into `raw_ticks` using psycopg3 `copy()` — *1000 ticks/batch arrive in DB in < 100ms*
- [ ] **[M2-M2]** Write synthetic data generator (`gen_ticks.py`) using `faker`+`numpy` producing realistic OHLCV patterns for 10 symbols — *Generates and inserts 1M rows in < 5 minutes*
- [ ] **[M2-M2]** Create all four continuous aggregates (`ohlcv_1m`, `ohlcv_5m`, `ohlcv_15m`, `ohlcv_1h`) with refresh policies — *`SELECT count(*) FROM ohlcv_1m` returns data within 2 minutes of tick insertion*
- [ ] **[M2-M2]** Implement VWAP SQL query as parameterized function — *Returns correct VWAP for test dataset*
- [ ] **[M2-M2]** Implement SMA-20 SQL window function query — *Returns correct 20-period SMA matching pandas baseline*
- [ ] **[M2-M2]** Implement Bollinger Bands SQL query (SMA ± 2σ) — *Upper/lower bands computed correctly for test series*
- [ ] **[M2-M2]** Implement RSI-14 SQL query using LAG + window AVG — *RSI values within ±0.5 of pandas-talib baseline*
- [ ] **[M2-M2]** Register analytics endpoints in FastAPI: `GET /analytics/ticks`, `GET /analytics/ohlcv`, `GET /analytics/indicators` — *All return valid JSON with correct pagination*
- [ ] **[M2-M2]** Run performance comparison query: same time-range SELECT on plain PostgreSQL table vs hypertable at 100K/500K/1M rows; record times in `benchmark_runs` — *Hypertable is ≥ 10× faster at 1M rows*

### Module 3 – Bellman-Ford Baseline

- [ ] **[M3-M3]** Implement `bellman_ford_arbitrage(rates_matrix, nodes)` function in Python — *Correctly detects known negative cycle in synthetic test graph*
- [ ] **[M3-M3]** Write graph population script: create 20+ Asset nodes in Apache AGE — *`SELECT * FROM cypher('fx_graph', $$ MATCH (n:Asset) RETURN n $$)...` returns ≥ 20 rows*
- [ ] **[M3-M3]** Write script to create EXCHANGE edges for all active trading pairs — *Cypher `MATCH ()-[r:EXCHANGE]->() RETURN count(r)` returns expected count*

---

## PHASE 2 – Intermediate Evaluation (Week 3: Mar 22–28)

### Module 2 (continued)

- [ ] **[M2-M2]** Verify all four OHLCV continuous aggregates refresh automatically on schedule — *Demo: insert new ticks, wait 1 min, confirm ohlcv_1m updated*
- [ ] **[M2-M2]** Verify compression policy activates correctly for 7-day-old chunks — *`SELECT * FROM chunk_compression_stats('raw_ticks')` shows compressed chunks*

### Module 3 – Apache AGE Graph

- [ ] **[M3-M3]** Implement 500ms background worker (`edge_weight_updater.py`) that polls LOB best-bids and updates AGE edge weights — *Edge `bid` property changes in DB within 600ms of LOB price movement*
- [ ] **[M3-M3]** Implement Cypher query to find all 3-hop profitable cycles from USD — *Returns correct cycles for synthetic rate matrix with known arbitrage*
- [ ] **[M3-M3]** Implement `GET /graph/nodes`, `GET /graph/edges`, `GET /graph/paths` API endpoints — *All return valid JSON*
- [ ] **[M3-M3]** Register Apache AGE graph metrics in Prometheus (edge count, update latency) — *Metrics visible in Grafana*

### Module 4 – Grover Circuit (8-node)

- [ ] **[M4-M4]** Implement `enumerate_3cycles(nodes)` to list all directional 3-hop cycles — *Returns correct count: P(N,3) = N!/(N-3)! cycles for N nodes*
- [ ] **[M4-M4]** Implement `is_profitable(cycle, rates)` predicate: product of three edge rates > 1.0 — *Unit test passes for known profitable and unprofitable cycles*
- [ ] **[M4-M4]** Build and test Grover oracle circuit for N=8 currency nodes — *Qiskit circuit runs; correct state measured with probability > 50%*
- [ ] **[M4-M4]** Build diffuser circuit and compose full Grover algorithm for N=8 — *Correct profitable cycle returned from simulated measurement*

### Module 5 – Security Proxy (begin)

- [ ] **[M5-M5]** Implement FastAPI SQL injection AST middleware using `sqlglot` — *Unit test: all OWASP Top-10 SQL injection payloads return HTTP 403*
- [ ] **[M5-M5]** Log all blocked requests to `security_events` table — *After test payloads, `SELECT count(*) FROM security_events WHERE event_type='SQL_INJECTION'` matches blocked count*

### Integration Milestone
- [ ] **[ALL]** Intermediate demo: show LOB engine accepting orders via REST, trades persisting to TimescaleDB, OHLCV aggregates populating, and depth WebSocket streaming — *All visible in terminal/Grafana during live demo*

---

## PHASE 3 – Quantum & Security Integration (Week 4: Mar 29 – Apr 7)

### Module 4 – Grover at Scale

- [ ] **[M4-M4]** Scale Grover circuit to N=16 nodes; verify correct detection — *Correct 3-hop cycle detected from 16-node test graph*
- [ ] **[M4-M4]** Scale Grover circuit to N=32 nodes using AerSimulator statevector method — *Circuit runs in < 60 seconds on local hardware*
- [ ] **[M4-M4]** Scale Grover circuit to N=64 nodes — *Runs or gracefully reports memory constraint with meaningful error*
- [ ] **[M4-M4]** Integrate Qiskit middleware with PostgreSQL: write detected arbitrage signals to `arbitrage_signals` table — *After `POST /quantum/run-grover`, row appears in DB with correct path and profit_pct*
- [ ] **[M4-M4]** Run benchmarking: Grover vs Bellman-Ford for N ∈ {8,16,32,64}; 10 trials each; compute mean ± std — *CSV of results saved as `benchmark_quantum.csv`*
- [ ] **[M4-M4]** Generate log-log scaling plot of Grover vs Bellman-Ford timing — *Plot saved as `quantum_scaling.png`; curves show expected divergence*
- [ ] **[M4-M4]** Implement `GET /quantum/signals` endpoint — *Returns recent signals from DB with correct fields*

### Module 5 – Complete Security Layer

- [ ] **[M5-M5]** Implement Redis sliding-window rate limiter middleware (1000 req/sec per IP) — *Unit test: 1001st request in 1 second returns HTTP 429*
- [ ] **[M5-M5]** Log rate-limit events to `security_events` table — *Events appear in DB after test*
- [ ] **[M5-M5]** Set up Prometheus scraping for all five targets (FastAPI, postgres-exporter, redis-exporter, node-exporter, lob-engine) — *All targets show UP in Prometheus*
- [ ] **[M5-M5]** Configure Grafana dashboard Panel 1: Candlestick chart from `ohlcv_1m` — *Live candlesticks visible updating every minute*
- [ ] **[M5-M5]** Configure Grafana Panel 2: Order book depth heatmap — *Heatmap shows bid/ask depth by price level*
- [ ] **[M5-M5]** Configure Grafana Panel 3: Volume bar chart from `ohlcv_1m` — *Volume bars update with new tick data*
- [ ] **[M5-M5]** Configure Grafana Panel 4: Arbitrage signals feed table — *New signals appear within seconds of Qiskit detection*
- [ ] **[M5-M5]** Configure Grafana Panel 5: System QPS and p99 latency time series from Prometheus — *Metrics update in real-time*
- [ ] **[M5-M5]** Run Siege DDoS simulation (`siege -c 1000 -t 60S`): confirm rate limiter holds, system returns to normal after load — *Grafana shows spike; `security_events` count increases; LOB engine stays responsive*

### Full Integration

- [ ] **[ALL]** End-to-end integration test: (1) ingest live/synthetic data → (2) LOB matches → (3) ticks persist → (4) AGE edges update → (5) Qiskit runs → (6) signal appears in Grafana — *All 5 stages complete without manual intervention*
- [ ] **[ALL]** Fix all integration bugs found during end-to-end test — *Re-run passes cleanly*

---

## PHASE 4 – Benchmarking, Report & Demo (Week 5: Apr 8–14)

- [ ] **[M1-M1]** Run final Siege QPS benchmark; target > 100,000 orders/sec; save results to `benchmark_runs` — *Results table shows peak_qps ≥ 100,000*
- [ ] **[M2-M2]** Run TimescaleDB vs plain PostgreSQL query benchmark at 1M rows; generate comparison table — *Hypertable query ≥ 10× faster documented in report*
- [ ] **[M4-M4]** Finalize quantum scaling plot and circuit diagram screenshots — *Images saved and embedded in final report*
- [ ] **[M5-M5]** Take Grafana dashboard screenshots for all 5 panels — *PNG files saved for report*
- [ ] **[ALL]** Write 10-page final report (sections: intro, M1, M2, M3, M4, M5, benchmarks, conclusions, references) — *Submitted PDF ≥ 10 pages with all required figures*
- [ ] **[ALL]** Prepare live demo script: ordered sequence of commands/clicks covering all 5 modules in ≤ 20 minutes — *Dry run completed without surprises*
- [ ] **[ALL]** Code freeze: merge `dev` → `main`; tag `v1.0.0`; verify `docker compose up` from clean state builds and runs everything — *Full system starts from scratch with one command*
- [ ] **[ALL]** Final submission: push code + report to GitHub; share link with instructor — *Submission confirmed before April 15, 2026*

---

## Appendix – File Structure

```
hqt/
├── docker-compose.yml
├── init.sql
├── requirements.txt
├── .env.example
├── module1_lob/
│   ├── order_book.py
│   ├── ring_buffer.py
│   ├── lob_api.py
│   └── bench_threadpool.py
├── module2_timescale/
│   ├── kafka_consumer.py
│   ├── gen_ticks.py
│   ├── indicators.sql
│   └── analytics_api.py
├── module3_graph/
│   ├── graph_init.py
│   ├── edge_weight_updater.py
│   ├── bellman_ford.py
│   └── graph_api.py
├── module4_quantum/
│   ├── grover_oracle.py
│   ├── grover_diffuser.py
│   ├── run_grover.py
│   ├── benchmark_quantum.py
│   └── quantum_api.py
├── module5_security/
│   ├── main.py              ← FastAPI proxy (entry point)
│   ├── sql_firewall.py
│   ├── rate_limiter.py
│   ├── prometheus_metrics.py
│   └── grafana_provisioning/
├── tests/
│   ├── test_lob.py
│   ├── test_timescale.py
│   ├── test_graph.py
│   ├── test_quantum.py
│   └── test_security.py
└── report/
    └── final_report.pdf
```
