# Database Schema Reference

## Hybrid Trading Database System

All tables reside in **PostgreSQL 16** with TimescaleDB and Apache AGE extensions loaded.

---

## 1. TimescaleDB Tables

### 1.1 `raw_ticks` (Hypertable)

```sql
CREATE TABLE raw_ticks (
    ts          TIMESTAMPTZ        NOT NULL,
    symbol      VARCHAR(20)        NOT NULL,
    price       NUMERIC(18, 8)     NOT NULL,
    volume      NUMERIC(18, 8)     NOT NULL,
    side        CHAR(1)            NOT NULL CHECK (side IN ('B','S')),
    order_id    UUID               NOT NULL,
    trade_id    UUID               NOT NULL,
    exchange    VARCHAR(30)        DEFAULT 'SYNTHETIC'
);

SELECT create_hypertable('raw_ticks', 'ts',
    partitioning_column => 'symbol',
    number_partitions   => 4,
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON raw_ticks (symbol, ts DESC);
```

**Compression policy** - applied after 7 days:

```sql
ALTER TABLE raw_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('raw_ticks', INTERVAL '7 days', if_not_exists => TRUE);
```

**Retention policy** - drop chunks older than 90 days:

```sql
SELECT add_retention_policy('raw_ticks', INTERVAL '90 days', if_not_exists => TRUE);
```

---

### 1.2 Continuous Aggregates - All Four Required

All four views are defined in `init.sql` inside idempotent `DO $$ IF NOT EXISTS $$` blocks.

#### `ohlcv_1m`

```sql
CREATE MATERIALIZED VIEW ohlcv_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts) AS bucket,
    symbol,
    FIRST(price, ts)            AS open,
    MAX(price)                  AS high,
    MIN(price)                  AS low,
    LAST(price, ts)             AS close,
    SUM(volume)                 AS volume
FROM raw_ticks
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1m',
    start_offset      => INTERVAL '10 minutes',
    end_offset        => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists     => TRUE);
```

#### `ohlcv_5m`

```sql
CREATE MATERIALIZED VIEW ohlcv_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', ts) AS bucket,
    symbol,
    FIRST(price, ts) AS open, MAX(price) AS high,
    MIN(price) AS low, LAST(price, ts) AS close,
    SUM(volume) AS volume
FROM raw_ticks
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_5m',
    start_offset      => INTERVAL '30 minutes',
    end_offset        => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists     => TRUE);
```

#### `ohlcv_15m`

```sql
CREATE MATERIALIZED VIEW ohlcv_15m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('15 minutes', ts) AS bucket,
    symbol,
    FIRST(price, ts) AS open, MAX(price) AS high,
    MIN(price) AS low, LAST(price, ts) AS close,
    SUM(volume) AS volume
FROM raw_ticks
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_15m',
    start_offset      => INTERVAL '1 hour',
    end_offset        => INTERVAL '15 minutes',
    schedule_interval => INTERVAL '15 minutes',
    if_not_exists     => TRUE);
```

#### `ohlcv_1h`

```sql
CREATE MATERIALIZED VIEW ohlcv_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS bucket,
    symbol,
    FIRST(price, ts) AS open, MAX(price) AS high,
    MIN(price) AS low, LAST(price, ts) AS close,
    SUM(volume) AS volume
FROM raw_ticks
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1h',
    start_offset      => INTERVAL '4 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists     => TRUE);
```

---

## 2. Order Book Tables

### 2.1 `orders`

```sql
CREATE TABLE IF NOT EXISTS orders (
    order_id     UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    symbol       VARCHAR(20)     NOT NULL,
    side         CHAR(1)         NOT NULL CHECK (side IN ('B','S')),
    order_type   VARCHAR(10)     NOT NULL CHECK (order_type IN ('LIMIT','MARKET')),
    price        NUMERIC(18, 8),
    quantity     NUMERIC(18, 8)  NOT NULL,
    filled_qty   NUMERIC(18, 8)  NOT NULL DEFAULT 0,
    status       VARCHAR(10)     NOT NULL DEFAULT 'OPEN'
                                 CHECK (status IN ('OPEN','PARTIAL','FILLED','CANCELLED')),
    client_id    VARCHAR(64),
    PRIMARY KEY (order_id)           -- single-column PK required for FK references
);

CREATE INDEX IF NOT EXISTS idx_orders_open
    ON orders (symbol, side, price DESC, ts ASC)
    WHERE status = 'OPEN';
```

### 2.2 `trades`

```sql
CREATE TABLE IF NOT EXISTS trades (
    trade_id      UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    symbol        VARCHAR(20)     NOT NULL,
    buy_order_id  UUID            NOT NULL REFERENCES orders(order_id),
    sell_order_id UUID            NOT NULL REFERENCES orders(order_id),
    price         NUMERIC(18, 8)  NOT NULL,
    quantity      NUMERIC(18, 8)  NOT NULL,
    PRIMARY KEY (trade_id)           -- single-column PK; ⚠ was (trade_id, ts) - FK refs fail with composite
);

SELECT create_hypertable('trades', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades (symbol, ts DESC);
```

---

## 3. Graph Database (Apache AGE)

### 3.1 Graph Initialization

```sql
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'fx_graph') THEN
        PERFORM ag_catalog.create_graph('fx_graph');
    END IF;
END $$;
```

### 3.2 Asset Nodes (20 total - via `module3_graph/graph_init.py`)

```cypher
-- Crypto (10)
MERGE (:Asset {symbol: 'BTC', name: 'Bitcoin',      asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'ETH', name: 'Ethereum',     asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'BNB', name: 'BNB',          asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'SOL', name: 'Solana',       asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'ADA', name: 'Cardano',      asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'XRP', name: 'Ripple',       asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'DOGE',name: 'Dogecoin',     asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'AVAX',name: 'Avalanche',    asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'MATIC',name:'Polygon',      asset_class: 'CRYPTO'})
MERGE (:Asset {symbol: 'DOT', name: 'Polkadot',     asset_class: 'CRYPTO'})
-- Fiat (10)
MERGE (:Asset {symbol: 'USD', name: 'US Dollar',    asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'EUR', name: 'Euro',         asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'GBP', name: 'Pound',        asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'JPY', name: 'Yen',          asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'AUD', name: 'AUD',          asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'CAD', name: 'CAD',          asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'CHF', name: 'Swiss Franc',  asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'INR', name: 'Indian Rupee', asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'SGD', name: 'SGD',          asset_class: 'FIAT'})
MERGE (:Asset {symbol: 'HKD', name: 'HKD',          asset_class: 'FIAT'})
```

### 3.3 Exchange Rate Edges

```cypher
MATCH (a:Asset {symbol: 'BTC'}), (b:Asset {symbol: 'USD'})
CREATE (a)-[:EXCHANGE {
    bid:          65000.0,
    ask:          65005.0,
    spread:       5.0,
    last_updated: timestamp()
}]->(b)
```

### 3.4 Live Edge Weight Update (every 500ms via `edge_weight_updater.py`)

```sql
SELECT * FROM cypher('fx_graph', $$
    MATCH (a:Asset {symbol: $from})-[r:EXCHANGE]->(b:Asset {symbol: $to})
    SET r.bid = $bid, r.ask = $ask, r.last_updated = timestamp()
$$, $1) AS (result agtype);
```

---

## 4. Arbitrage Signals Table

```sql
CREATE TABLE IF NOT EXISTS arbitrage_signals (
    signal_id         UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts                TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    path              TEXT[]          NOT NULL,   -- e.g. ARRAY['USD','BTC','ETH','USD']
    profit_pct        NUMERIC(10, 6)  NOT NULL,
    method            VARCHAR(20)     NOT NULL
                                      CHECK (method IN ('QUANTUM','CLASSICAL')),
    circuit_depth     INT,                        -- Grover only
    grover_iterations INT,                        -- Grover only
    classical_ms      NUMERIC(10, 3),             -- Bellman-Ford wall-clock
    quantum_ms        NUMERIC(10, 3),             -- Grover wall-clock
    graph_size_n      INT             NOT NULL,
    PRIMARY KEY (signal_id)            -- ⚠ was (signal_id, ts) composite - fixed
);

SELECT create_hypertable('arbitrage_signals', 'ts', if_not_exists => TRUE);
```

**Architecture note:** `method='CLASSICAL'` rows are inserted every 500ms by the Bellman-Ford background loop (primary production algorithm). `method='QUANTUM'` rows are inserted every 10s by the Grover benchmark loop (research only). Grafana Panel 4 displays both streams colour-coded.

---

## 5. Security & Observability Tables

### 5.1 `security_events`

```sql
CREATE TABLE IF NOT EXISTS security_events (
    event_id     UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    client_ip    INET            NOT NULL,
    event_type   VARCHAR(30)     NOT NULL
                                 CHECK (event_type IN ('SQL_INJECTION','RATE_LIMIT','AUTH_FAIL')),
    raw_payload  TEXT,
    blocked      BOOLEAN         NOT NULL DEFAULT TRUE,
    endpoint     VARCHAR(200),
    PRIMARY KEY (event_id)           -- ⚠ was (event_id, ts) composite - fixed
);

SELECT create_hypertable('security_events', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_security_ip_ts ON security_events (client_ip, ts DESC);
```

### 5.2 `benchmark_runs`

```sql
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts               TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    tool             VARCHAR(30)     NOT NULL,   -- 'SIEGE' | 'PYTHON_THREADPOOL' | 'TIMESCALE_BENCH' | 'QUANTUM_BENCH'
    target_endpoint  VARCHAR(200)    NOT NULL,
    duration_sec     INT             NOT NULL,
    concurrent_users INT             NOT NULL,
    total_requests   BIGINT,
    successful_reqs  BIGINT,
    failed_reqs      BIGINT,
    peak_qps         NUMERIC(12, 2),
    avg_latency_ms   NUMERIC(10, 3),
    p99_latency_ms   NUMERIC(10, 3),
    notes            TEXT
);
```

---

## 6. Schema Change Log

| Version | Change | Reason |
|---------|--------|--------|
| v1.0 (initial) | `trades PRIMARY KEY (trade_id, ts)` | TimescaleDB hypertable convention |
| v1.1 (fixed) | `trades PRIMARY KEY (trade_id)` | Composite PK breaks `REFERENCES orders(order_id)` FK on INSERT |
| v1.0 (initial) | `arbitrage_signals PRIMARY KEY (signal_id, ts)` | TimescaleDB hypertable convention |
| v1.1 (fixed) | `arbitrage_signals PRIMARY KEY (signal_id)` | Composite PK unnecessary; hypertable indexed on `ts` separately |
| v1.0 (initial) | `security_events PRIMARY KEY (event_id, ts)` | TimescaleDB hypertable convention |
| v1.1 (fixed) | `security_events PRIMARY KEY (event_id)` | Same reason as above |
| v1.0 (initial) | Only `ohlcv_1m` continuous aggregate existed | Omission |
| v1.1 (fixed) | Added `ohlcv_5m`, `ohlcv_15m`, `ohlcv_1h` | Required by `GET /analytics/ohlcv?interval=5m|15m|1h` endpoint |
