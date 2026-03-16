# High-Frequency Trading (HQT) System Architecture & Data Flow

This document provides a comprehensive breakdown of the entire HQT arbitrage detection system. It explains the responsibilities, technologies, and data flow across **Module 1 (LOB Engine)**, **Module 2 (TimescaleDB Data Layer)**, and **Module 3 (Graph Arbitrage Engine)**.

---

## 🏗️ High-Level System Overview

The goal of this system is to detect **real-time cyclic arbitrage** across cryptocurrency pairs and fiat currencies. It accomplishes this by combining a low-latency C++ limit order book (to track Bid/Ask spreads), a high-performance time-series database (to track historical trades and provide price fallbacks), and a Graph database running the Bellman-Ford algorithm to find negative-weight cycles representing profitable multi-hop trades.

### The Technologies Used:
1. **C++ (Drogon Framework):** Powers the ultra-fast Limit Order Book (LOB) matching engine.
2. **Python:** Powers all data ingestion, the graph engine, API endpoints, and backfillers using `asyncio` and `FastAPI`.
3. **Apache Kafka & Zookeeper:** High-throughput message broker handling the firehose of real-time trades.
4. **TimescaleDB (PostgreSQL):** Time-series database for storing raw tick data and generating continuous `OHLCV` (Open, High, Low, Close, Volume) candlestick aggregates.
5. **Apache AGE (PostgreSQL Extension):** Graph database overlay enabling Cypher queries and directed graph modeling for assets and exchange rates.
6. **Grafana:** Dashboarding tool visualizing time-series aggregates, arbitrage signals, and system health metrics.

---

## 📦 Module 1: Limit Order Book (LOB) Engine
**Role:** Maintain the real-time state of pending orders (Bids and Asks) to provide accurate market spread data.

### Components & Technologies:
*   **`lob_server` (C++ / Drogon):** A heavily optimized, passive matching engine. It exposes HTTP REST endpoints (`POST /lob/order`, `GET /lob/depth/{symbol}`). It processes limit and market orders using striped mutexes and concurrent lock-free queues for maximum performance.
*   **`kraken_feeder.py` (Python / websockets):** A bridge script. Since the C++ LOB is "passive" (it waits for orders to be submitted rather than scraping them itself), this script connects to Kraken's **Level 2 WebSocket feed**. It streams live Bids and Asks for 10 crypto pairs and immediately `POST`s them to the C++ LOB engine as synthetic limit orders.
*   **Kafka Publisher:** When trades execute inside the LOB, it publishes `executed_trades` events to Kafka.

### Data Flow out of Module 1:
1. `kraken_feeder.py` reads Kraken L2 WebSocket arrays of bids/asks every few milliseconds.
2. It pushes them into the C++ `lob_server`.
3. Module 3 queries `GET /lob/depth/{symbol}` to grab the absolute latest, live Bid/Ask spread for a pair.

---

## 📦 Module 2: Timescale Data Layer & Ingestion
**Role:** Ingest, store, backfill, and aggregate *Completed Trades* for historical analytics, charting, and failsafe price fallbacks.

### Components & Technologies:
*   **TimescaleDB (Postgres):** Stores individual trades in a massive hypertable (`raw_ticks`). It uses **Continuous Aggregates** to automatically bucket those millions of rows into 1m, 5m, 15m, and 1h `ohlcv` tables for lightning-fast Grafana charts without querying the raw table.
*   **`live_streamer.py` (Python / Kafka):** Connects to Kraken's WebSocket *Trade channel* (executed trades, not the order book). It streams 100s of actual market trades per second straight into Kafka to feed the database.
*   **`kafka_consumer.py` (Python / psycopg):** Reads the trades off the Kafka topic and does high-speed bulk inserts (`executemany`) into TimescaleDB.
*   **`smart_backfiller.py` & `fetch_real_data.py`:** If the system goes offline, there will be gaps in the data payload. The smart backfiller scans the hypertable daily/hourly to find missing time windows, connects to the Kraken REST API, pulls the historical data for just that specific gap, and backfills it so charts are flawless.
*   **`analytics_api.py`:** A FastAPI application exposing endpoints to query history, OHLCV aggregates, and monitor system ingestion health.

### Data Flow out of Module 2:
1. Live trade data flows from Kraken WS → Kafka → DB.
2. Grafana directly queries TimescaleDB to render volume and candlestick charts.
3. Module 3 uses this DB as a **Fallback Source**. If Module 1's order book is empty or offline, Module 3 queries the last recorded trade price from TimescaleDB so the arbitrage engine never stops running.

---

## 📦 Module 3: Graph Arbitrage Engine
**Role:** Model all assets as a connected network and mathematically detect the most profitable sequence of trades.

### Components & Technologies:
*   **Apache AGE (PostgreSQL Extension):** Models the financial world as a Graph. 
    *   **Nodes (20):** 10 Cryptocurrencies + 10 Fiat currencies.
    *   **Edges (380):** Directed relationships between nodes (e.g., `BTC → USD`, `USD → EUR`, `ETH → BTC`) storing the current exchange rate.
*   **`graph_init.py`:** Bootstraps the system. If the `fx_graph` doesn't exist, it creates it, initializes all 20 nodes, and creates the default edges querying initial snapshot prices from Alpha Vantage and Kraken.
*   **`edge_weight_updater.py` (Python):** The heartbeat of the graph. It runs a loop every ~500ms:
    1. It queries **Module 1 (LOB Engine)** for the live Bid/Ask spread of cryptos. Consumes fallback data from **Module 2** if needed.
    2. It updates all 380 edges in the graph using Cypher `MATCH / SET` commands via `psycopg`.
    3. It mathematically transforms the rates into `-log(rate)`. (Adding logarithms is equal to multiplying the raw rates, which makes Bellman-Ford addition compatible).
    4. Also polls the ECB (European Central Bank) / Alpha Vantage for fiat cross-rates (EUR, JPY, GBP, etc.) and updates the Fiat↔Fiat and Fiat↔Crypto edges.
*   **`bellman_ford.py` (Python):** The detector. Because edge weights are mapped as `-log(rate)`, any path through the graph that results in a **negative total sum** represents a **profitable arbitrage cycle**.
    *   It queries the full Adjacency Matrix from AGE.
    *   Runs the Bellman-Ford algorithm in under 5 milliseconds.
    *   If it finds a cycle (e.g., `LINK → SOL → ADA → XRP → AVAX → HKD → BTC → ETH → LINK`), it calculates the exact profit percentage and writes the signal to the `arbitrage_signals` SQL table.

### Data Flow out of Module 3:
1. Bids/Asks flow in from LOB Engine. Fiat rates flow in from APIs.
2. Edges are constantly updated in real-time.
3. Bellman-Ford spots a loop where `Total Output > Total Input` minus the friction of the Bid/Ask spread.
4. The signal is dumped into PostgreSQL.
5. Grafana displays the Profit heatmap, Unique Cycles, and Time-series profit tracking based on these signals.

---

## 🔄 The Complete End-to-End Data Flow

1. **Market moves on Kraken.**
2. **Module 2 (`live_streamer`)** catches the *executed trade* and sends it to Kafka. **Module 1 (`kraken_feeder`)** catches the *order book depth change* and sends it to the C++ LOB.
3. **Module 2 Kafka Consumer** inserts the trade into `raw_ticks`. TimescaleDB updates the 1m/5m/1h `ohlcv` candlestick aggregates. 
4. **Module 3 Edge Updater** asks Module 1: "What are the new Bid/Ask limits?" It gets the new LOB depth and updates the graph edges in Apache AGE.
5. **Module 3 Bellman-Ford** scans the 380 edges. It sees the new spread has temporarily created an inefficiency between `DOT`, `EUR`, and `BTC`.
6. Negative-cycle detected! It calculates `profit = 1.28%` and writes the path to `arbitrage_signals`.
7. **Grafana Dashboard** refreshes (every 5s). It renders the new candlestick from Module 2, and plots the new 1.28% arbitrage signal from Module 3.

*End of Document.*
