# HQT Live Demo Script

**Duration:** ~20 minutes  
**Audience:** CS39006 DBMS Lab professors  
**Setup:** `docker compose up -d` running, Grafana open at `http://localhost:3000`

---

## 0 — Pre-demo checklist (5 min before)

```bash
docker compose ps                          # all services must show healthy/running
docker compose logs --tail=20 lob-engine  # no fatal errors
docker compose logs --tail=20 graph-service
```

Open tabs: Grafana (:3000), this script.

---

## 1 — Show all 5 modules are live (2 min)

```bash
# Verify all services healthy
docker compose ps --format "table {{.Name}}\t{{.Status}}"
```

Expected: 12 services, all `Up` or `healthy`.

```bash
# Module 1 — LOB engine
curl -s http://localhost:8001/lob/health | python3 -m json.tool
```
Expected: `{"status": "ok", ...}`

```bash
# Module 2 — TimescaleDB analytics
curl -s http://localhost:8002/analytics/health | python3 -m json.tool
```
Expected: `{"status": "ok", "row_count": <N>}`

```bash
# Module 3 — Graph service
curl -s http://localhost:8003/graph/health | python3 -m json.tool
```
Expected: `{"status": "ok", "node_count": 20, ...}`

```bash
# Module 4 — Quantum engine
curl -s http://localhost:8004/health | python3 -m json.tool
```
Expected: `{"status": "ok"}`

```bash
# Module 5 — Security proxy (routes all traffic)
curl -s http://localhost:8000/health | python3 -m json.tool
```

---

## 2 — Place LOB orders and show depth (3 min)

```bash
# Place a passive sell
curl -s -X POST http://localhost:8000/lob/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USD","side":"A","ordertype":"LIMIT","price":65000.00,"quantity":2.0,"client_id":"demo_sell"}' \
  | python3 -m json.tool

# Place a crossing buy (triggers a trade)
curl -s -X POST http://localhost:8000/lob/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USD","side":"B","ordertype":"LIMIT","price":65100.00,"quantity":1.0,"client_id":"demo_buy"}' \
  | python3 -m json.tool

# Show updated order book depth
curl -s http://localhost:8000/lob/depth/BTC%2FUSD | python3 -m json.tool
```

**Talk track:** "Module 1 is a C++20 matching engine using a Red-Black Tree keyed on price. The crossing order triggered a trade which was published to Kafka and consumed by Module 2."

---

## 3 — Show TimescaleDB ingestion (2 min)

```bash
# Count rows in raw_ticks (should be growing)
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT COUNT(*), MAX(ts) FROM raw_ticks;"

# Show OHLCV continuous aggregate
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT bucket, open, high, low, close, volume FROM ohlcv_1m WHERE symbol='BTC/USD' ORDER BY bucket DESC LIMIT 5;"
```

**Talk track:** "Module 2 uses a TimescaleDB hypertable partitioned by day and symbol. Continuous aggregates materialise 1m/5m/15m/1h OHLCV automatically. Our benchmark showed 38× faster queries versus plain PostgreSQL on 1 million rows."

Switch to Grafana → scroll to candlestick panel.

---

## 4 — Show live arbitrage signals (3 min)

```bash
# Query arbitrage_signals directly
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT ts, path, ROUND(profit_pct::numeric, 4) AS profit_pct, method, classical_ms FROM arbitrage_signals ORDER BY ts DESC LIMIT 5;"
```

Switch to Grafana → scroll to **Graph Arbitrage Engine** section → show the timeline panel updating.

**Talk track:** "Module 3 maintains a 20-node Apache AGE graph of FX exchange rates updated every 500ms from the LOB. Bellman-Ford runs on this graph every 500ms using a −log(rate) weight transformation — a negative cycle in the transformed graph means a profitable arbitrage cycle in the real market."

---

## 5 — Security demo: SQL injection blocked (3 min)

```bash
# Attempt SQL injection in the order symbol field
curl -s -X POST http://localhost:8000/lob/order \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC/USD; DROP TABLE raw_ticks;--","side":"B","price":1,"quantity":1}' \
  -w "\nHTTP Status: %{http_code}\n"
```

Expected: `HTTP Status: 403`

```bash
# Confirm the event was logged
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT ts, event_type, client_ip, blocked FROM security_events ORDER BY ts DESC LIMIT 3;"
```

Switch to Grafana → **Security & Observability** section → show the SQL Injections Blocked counter increment.

**Talk track:** "Module 5 is a FastAPI reverse proxy. The SQL firewall uses sqlglot to parse the AST of any string that looks like SQL — it caught the DROP TABLE attempt before it reached any backend service."

---

## 6 — Quantum benchmark (3 min)

Switch to Grafana → **Quantum Engine** section → show the benchmark table.

**Talk track:** "Module 4 runs Grover's Algorithm on the same rate matrix that Bellman-Ford uses. At N=32 nodes: Bellman-Ford completes in 3.5ms, Grover takes 20,373ms on AerSimulator — 5,848× slower. This is because AerSimulator maintains a 65,536-element state vector classically. On real quantum hardware, the same circuit would execute in O(√N) oracle calls, providing a quadratic speedup. The benchmark quantifies exactly what near-term quantum advantage we'd need to unlock."

Open `module4_quantum/bench_out/benchmark_quantum.png` alongside the live Grafana table.

---

## 7 — Hero row summary (2 min)

Scroll Grafana back to the top → **System Overview** row.

Point to each tile:
1. **LOB Throughput** — live orders/sec from Prometheus
2. **TimescaleDB Speedup** — 37× from benchmark data
3. **Arb Signals (24h)** — CLASSICAL signals detected today
4. **Grover Overhead @N=32** — 5,848× from benchmark data
5. **SQL Injections Blocked** — 1 (the one we just fired)
6. **Services Up** — 5/5 modules healthy

**Talk track:** "All five modules — LOB, TimescaleDB, AGE graph, quantum engine, security proxy — are running and observable from a single Grafana dashboard."

---

## Appendix — Useful commands during Q&A

```bash
# Show graph nodes
curl -s http://localhost:8003/graph/nodes | python3 -m json.tool | head -40

# Show recent quantum signals
curl -s "http://localhost:8004/quantum/signals?limit=5&method=QUANTUM" | python3 -m json.tool

# Show rate matrix (N×N)
curl -s http://localhost:8003/graph/rates | python3 -m json.tool | head -20

# Show Prometheus metrics from LOB
curl -s http://localhost:8001/lob/metrics | grep lob_orders

# Show compression stats
docker compose exec postgres psql -U hqt -d hqt -c \
  "SELECT * FROM chunk_compression_stats('raw_ticks') LIMIT 5;"
```
