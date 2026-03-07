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

SELECT create_hypertable('raw_ticks', 'ts', partitioning_column => 'symbol',
    number_partitions => 4, chunk_time_interval => INTERVAL '1 day');

CREATE INDEX ON raw_ticks (symbol, ts DESC);
```

### 1.2 Continuous Aggregate – `ohlcv_1m`
```sql
CREATE MATERIALIZED VIEW ohlcv_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', ts)   AS bucket,
    symbol,
    FIRST(price, ts)              AS open,
    MAX(price)                    AS high,
    MIN(price)                    AS low,
    LAST(price, ts)               AS close,
    SUM(volume)                   AS volume
FROM raw_ticks
GROUP BY bucket, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('ohlcv_1m',
    start_offset => INTERVAL '10 minutes',
    end_offset   => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute');
```

### 1.3 Continuous Aggregates – `ohlcv_5m`, `ohlcv_15m`, `ohlcv_1h`
*(Same pattern as ohlcv_1m with respective time_bucket intervals)*

---

## 2. Order Book Tables

### 2.1 `orders`
```sql
CREATE TABLE orders (
    order_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    symbol       VARCHAR(20)     NOT NULL,
    side         CHAR(1)         NOT NULL CHECK (side IN ('B','S')),
    order_type   VARCHAR(10)     NOT NULL CHECK (order_type IN ('LIMIT','MARKET')),
    price        NUMERIC(18, 8),
    quantity     NUMERIC(18, 8)  NOT NULL,
    filled_qty   NUMERIC(18, 8)  NOT NULL DEFAULT 0,
    status       VARCHAR(10)     NOT NULL DEFAULT 'OPEN'
                                 CHECK (status IN ('OPEN','PARTIAL','FILLED','CANCELLED')),
    client_id    VARCHAR(64)
);

CREATE INDEX ON orders (symbol, side, price DESC, ts ASC) WHERE status = 'OPEN';
```

### 2.2 `trades`
```sql
CREATE TABLE trades (
    trade_id      UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    symbol        VARCHAR(20)     NOT NULL,
    buy_order_id  UUID            NOT NULL REFERENCES orders(order_id),
    sell_order_id UUID            NOT NULL REFERENCES orders(order_id),
    price         NUMERIC(18, 8)  NOT NULL,
    quantity      NUMERIC(18, 8)  NOT NULL
);

SELECT create_hypertable('trades', 'ts');
CREATE INDEX ON trades (symbol, ts DESC);
```

---

## 3. Graph Database (Apache AGE)

### 3.1 Graph Initialization
```sql
SELECT create_graph('fx_graph');
```

### 3.2 Asset Nodes
```cypher
-- Run via Apache AGE cypher() wrapper
CREATE (:Asset {symbol: 'BTC', name: 'Bitcoin', asset_class: 'CRYPTO'})
CREATE (:Asset {symbol: 'ETH', name: 'Ethereum', asset_class: 'CRYPTO'})
CREATE (:Asset {symbol: 'USD', name: 'US Dollar', asset_class: 'FIAT'})
CREATE (:Asset {symbol: 'EUR', name: 'Euro', asset_class: 'FIAT'})
CREATE (:Asset {symbol: 'INR', name: 'Indian Rupee', asset_class: 'FIAT'})
-- ... 20+ nodes total
```

### 3.3 Exchange Rate Edges
```cypher
MATCH (a:Asset {symbol: 'BTC'}), (b:Asset {symbol: 'USD'})
CREATE (a)-[:EXCHANGE {
    bid: 65000.0,
    ask: 65005.0,
    spread: 5.0,
    last_updated: timestamp()
}]->(b)
```

### 3.4 Edge Weight Update (background worker every 500ms)
```sql
SELECT * FROM cypher('fx_graph', $$
    MATCH (:Asset {symbol: $from})-[r:EXCHANGE]->(:Asset {symbol: $to})
    SET r.bid = $new_bid, r.last_updated = timestamp()
$$, $1) AS (result agtype);
```

---

## 4. Arbitrage Signals Table

```sql
CREATE TABLE arbitrage_signals (
    signal_id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    path                TEXT[]          NOT NULL,   -- e.g. ARRAY['USD','BTC','ETH','USD']
    profit_pct          NUMERIC(10, 6)  NOT NULL,
    method              VARCHAR(20)     NOT NULL CHECK (method IN ('QUANTUM','CLASSICAL')),
    circuit_depth       INT,                        -- Qiskit only
    grover_iterations   INT,                        -- Qiskit only
    classical_ms        NUMERIC(10, 3),
    quantum_ms          NUMERIC(10, 3),
    graph_size_n        INT             NOT NULL
);

SELECT create_hypertable('arbitrage_signals', 'ts');
```

---

## 5. Security & Observability Tables

### 5.1 `security_events`
```sql
CREATE TABLE security_events (
    event_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    client_ip    INET            NOT NULL,
    event_type   VARCHAR(30)     NOT NULL CHECK (event_type IN ('SQL_INJECTION','RATE_LIMIT','AUTH_FAIL')),
    raw_payload  TEXT,
    blocked      BOOLEAN         NOT NULL DEFAULT TRUE,
    endpoint     VARCHAR(200)
);

SELECT create_hypertable('security_events', 'ts');
CREATE INDEX ON security_events (client_ip, ts DESC);
```

### 5.2 `benchmark_runs`
```sql
CREATE TABLE benchmark_runs (
    run_id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    tool            VARCHAR(30)     NOT NULL,   -- 'SIEGE' | 'PYTHON_THREADPOOL'
    target_endpoint VARCHAR(200)    NOT NULL,
    duration_sec    INT             NOT NULL,
    concurrent_users INT            NOT NULL,
    total_requests  BIGINT,
    successful_reqs BIGINT,
    failed_reqs     BIGINT,
    peak_qps        NUMERIC(12, 2),
    avg_latency_ms  NUMERIC(10, 3),
    p99_latency_ms  NUMERIC(10, 3),
    notes           TEXT
);
```

---

## 6. Index & Compression Policies

```sql
-- Compression on raw_ticks after 7 days
ALTER TABLE raw_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('raw_ticks', INTERVAL '7 days');

-- Retention: drop raw_ticks older than 90 days
SELECT add_retention_policy('raw_ticks', INTERVAL '90 days');

-- Retention: keep arbitrage_signals forever (small table)
```
