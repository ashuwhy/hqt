# HQT Live Demo Script

**Duration:** ~20 minutes
**Audience:** CS39006 DBMS Lab professors
**Setup:** `docker compose up -d` running, Grafana open at `http://localhost:3000`

---

## 0 - Pre-demo checklist (5 min before)

```bash
# All services must be running
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

Expected: 12+ containers, all `Up` or `healthy`.

```bash
# Quick sanity check — all modules responding
curl -s http://localhost:8001/lob/health
curl -s http://localhost:8002/health
curl -s http://localhost:8003/graph/health
curl -s http://localhost:8004/health
curl -s http://localhost:8000/health
```

Open tabs: Grafana (:3000) on the hero row, this script.

---

## 1 - Architecture overview (1 min, no commands)

Point at the architecture diagram or draw on whiteboard:

> "HQT is a polyglot persistence system. Each of the five modules uses a different database technology chosen for its specific data access pattern. Live prices enter via the Kraken WebSocket. All external traffic routes through a security proxy."

- Module 1: C++ in-memory Red-Black Tree + Kafka
- Module 2: TimescaleDB hypertable (38× faster than plain PostgreSQL)
- Module 3: Apache AGE property graph inside PostgreSQL
- Module 4: Qiskit Grover circuit on AerSimulator (research benchmark)
- Module 5: FastAPI proxy with sqlglot SQL firewall + Redis rate limiter

---

## 2 - Live LOB and Kraken data (2 min)

```bash
# Show live order book depth — real Kraken prices
curl -s http://localhost:8001/lob/depth/BTCUSD | python3 -m json.tool | head -20
```

Expected: Top 10 bid/ask levels with real BTC prices (~$66,000+).

```bash
# Place a buy order through the security proxy (port 8000, not 8001)
curl -s -X POST http://localhost:8000/lob/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTCUSD","side":"B","ordertype":"LIMIT","price":65000.00,"quantity":0.01,"client_id":"demo"}' \
  | python3 -m json.tool
```

Expected: HTTP 201, `{"status":"success"}`.

> "Module 1 is a C++20 matching engine using a Red-Black Tree keyed on price. The Kraken feeder is posting real L2 order book data to the engine at ~500 orders/second right now. Our siege benchmark showed 3,211 orders/second sustained under 200 concurrent clients with 100% availability and zero failures."

Switch to Grafana hero row — point at **LOB Throughput** stat tile showing live req/s.

---

## 3 - TimescaleDB ingestion and speedup (2 min)

```bash
# Show raw_ticks row count — should be growing
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT COUNT(*), MAX(ts) as latest FROM raw_ticks;"

# Show OHLCV continuous aggregate — auto-refreshed from raw_ticks
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT bucket, open, high, low, close, volume
   FROM ohlcv_1m WHERE symbol='BTC/USD'
   ORDER BY bucket DESC LIMIT 5;"
```

> "Module 2 uses a TimescaleDB hypertable partitioned by day and symbol. The query you just saw hit a materialised continuous aggregate — it auto-refreshes as new ticks arrive. Our benchmark on 1 million rows showed 38× faster range queries compared to plain PostgreSQL. The speedup comes from chunk exclusion: a 1-hour query skips 23 of 24 daily partitions entirely."

Switch to Grafana — scroll to **Price Analysis** row, show the live candlestick chart.

---

## 4 - Live arbitrage detection (3 min)

```bash
# Show the most recent arbitrage signals
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT ts, path, ROUND(profit_pct::numeric,4) AS profit_pct, classical_ms, method
   FROM arbitrage_signals
   ORDER BY ts DESC LIMIT 5;"
```

Expected: CLASSICAL signals with realistic profits (0.001%–0.02%), cycles like `{USD,BTC,ETH,USD}`.

> "Module 3 maintains a 20-node Apache AGE property graph of live FX exchange rates. AGE is a PostgreSQL extension — graph traversals run inside standard PostgreSQL transactions. Bellman-Ford runs every 500ms using a negative-log weight transform: a negative cycle in the transformed graph corresponds to a profitable arbitrage cycle in the real market. At N=20 nodes with 380 edges, one complete run takes under 5ms."

Show the weight transform on a whiteboard if asked:
- w(i→j) = -log(rate(i→j))
- Negative cycle where sum of weights < 0 means product of rates > 1.0 (profit)

Switch to Grafana — **Graph Arbitrage Engine** row, show the live signal timeline.

---

## 5 - Security demo: SQL injection blocked (3 min)

```bash
# Attempt SQL injection — DROP TABLE in the symbol field
curl -s -X POST http://localhost:8000/lob/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USD; DROP TABLE raw_ticks;--","side":"B","price":1,"quantity":1}' \
  -w "\nHTTP Status: %{http_code}\n"
```

Expected: `HTTP Status: 403`

```bash
# Confirm the attack was logged
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT ts, event_type, client_ip, blocked
   FROM security_events ORDER BY ts DESC LIMIT 3;"
```

> "Module 5 is a FastAPI reverse proxy — the single entry point for all external traffic. The SQL firewall runs two layers: first, 15 regex patterns scan for injection keywords; second, sqlglot parses the input as SQL and walks the AST for DDL node types — Drop, Truncate, Create, AlterTable. The DROP TABLE was caught at layer 2. The event is logged to a TimescaleDB hypertable for time-range audit queries."

Switch to Grafana — **Security & Observability** row, show the **SQL Injections Blocked** counter increment.

---

## 6 - DDoS benchmark (1 min, show results)

Show `report/siege_ddos_results.txt` — do not re-run siege during the demo.

```bash
cat report/siege_ddos_results.txt
```

> "We ran 200 concurrent clients against the security proxy for 30 seconds. 100% availability, zero dropped requests. The 1,194ms response time reflects the full pipeline: SQL firewall scan, AST parse, Redis rate check, HTTP forward, response relay."

---

## 7 - Quantum benchmark (3 min)

Switch to Grafana — **Quantum Engine** row, show the benchmark table.

> "Module 4 runs Grover's Algorithm on the same rate matrix that Bellman-Ford uses. Both algorithms write to the same `arbitrage_signals` table — the `method` column distinguishes CLASSICAL from QUANTUM results."

Point at the table:
- At N=32: BF = 3.5ms, Grover on AerSimulator = 20,373ms — 5,848× slower

> "AerSimulator maintains a 65,536-element complex state vector classically. Every gate application is an O(2^n) matrix multiplication. This is not a property of Grover's algorithm — it's a property of classical simulation. On real quantum hardware with native gate execution, the same Grover circuit runs in O(sqrt(N)) oracle calls. Our benchmark chart is honest: it shows what near-term quantum tooling actually costs today, not what the theory promises."

Open `module4_quantum/bench_out/benchmark_quantum.png` side-by-side with Grafana.

---

## 8 - Hero row summary (2 min)

Scroll Grafana back to the top — **System Overview** hero row.

Point at each tile:
1. **LOB Throughput** — live orders/sec (Prometheus `rate(lob_orders_total[1m])`)
2. **TimescaleDB Speedup** — 38× (from `benchmark_quantum_results` table)
3. **Arb Signals (24h)** — live count of CLASSICAL signals today
4. **Grover Overhead @N=32** — 5,848× (from `benchmark_quantum_results`)
5. **SQL Injections Blocked** — at least 1 (the one we just fired)
6. **Services Up** — should show 5 or 6

> "All five modules — LOB engine, TimescaleDB analytics, AGE graph, quantum benchmark, security proxy — are running and observable from a single provisioned Grafana dashboard. Every panel is backed by a live data source."

---

## Appendix - Q&A commands

```bash
# Show graph nodes
curl -s http://localhost:8003/graph/nodes | python3 -m json.tool | head -40

# Show live N×N rate matrix (used by both BF and Grover)
curl -s http://localhost:8003/graph/rates | python3 -m json.tool | head -20

# Show recent quantum signals
curl -s "http://localhost:8004/quantum/signals?limit=5&method=QUANTUM" \
  | python3 -m json.tool

# Show Prometheus LOB metrics
curl -s http://localhost:8001/lob/metrics | grep lob_orders

# Show TimescaleDB chunk info
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT chunk_name, range_start, range_end,
          pg_size_pretty(total_bytes) AS size
   FROM chunk_detailed_size('raw_ticks') LIMIT 10;"

# Show compression stats
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT hypertable_name, compression_enabled,
          pg_size_pretty(before_compression_total_bytes) AS before,
          pg_size_pretty(after_compression_total_bytes) AS after
   FROM hypertable_compression_stats('raw_ticks');"

# Show arbitrage signals count by method
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT method, COUNT(*), ROUND(AVG(profit_pct)::numeric,4) AS avg_profit_pct
   FROM arbitrage_signals GROUP BY method;"

# Show AGE graph edge count
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT * FROM cypher('fx_graph',
     \$\$ MATCH ()-[e:EXCHANGE]->() RETURN count(e) AS edges \$\$
   ) AS (edges agtype);"
```
