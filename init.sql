-- HQT Database Initialization
-- Loads required extensions and creates initial table structure

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS age;

-- Ensure Apache AGE is in the search path
SET search_path = ag_catalog, public;

-- 1. TimescaleDB Tables
CREATE TABLE IF NOT EXISTS raw_ticks (
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
    number_partitions => 4, chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_ticks_symbol_ts ON raw_ticks (symbol, ts DESC);

-- 2. Order Book Tables
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
    PRIMARY KEY (order_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_open ON orders (symbol, side, price DESC, ts ASC) WHERE status = 'OPEN';

CREATE TABLE IF NOT EXISTS trades (
    trade_id      UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts            TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    symbol        VARCHAR(20)     NOT NULL,
    buy_order_id  UUID            NOT NULL REFERENCES orders(order_id),
    sell_order_id UUID            NOT NULL REFERENCES orders(order_id),
    price         NUMERIC(18, 8)  NOT NULL,
    quantity      NUMERIC(18, 8)  NOT NULL,
    PRIMARY KEY (trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trades_symbol_ts ON trades (symbol, ts DESC);

-- 3. Graph Database (Apache AGE)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'fx_graph') THEN
        PERFORM ag_catalog.create_graph('fx_graph');
    END IF;
END $$;

-- 4. Arbitrage Signals Table
CREATE TABLE IF NOT EXISTS arbitrage_signals (
    signal_id           UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts                  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    path                TEXT[]          NOT NULL,
    profit_pct          NUMERIC(10, 6)  NOT NULL,
    method              VARCHAR(20)     NOT NULL CHECK (method IN ('QUANTUM','CLASSICAL')),
    circuit_depth       INT,
    grover_iterations   INT,
    classical_ms        NUMERIC(10, 3),
    quantum_ms          NUMERIC(10, 3),
    graph_size_n        INT             NOT NULL,
    PRIMARY KEY (signal_id)
);

-- 5. Security & Observability Tables
CREATE TABLE IF NOT EXISTS security_events (
    event_id     UUID            NOT NULL DEFAULT gen_random_uuid(),
    ts           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    client_ip    INET            NOT NULL,
    event_type   VARCHAR(30)     NOT NULL CHECK (event_type IN ('SQL_INJECTION','RATE_LIMIT','AUTH_FAIL')),
    raw_payload  TEXT,
    blocked      BOOLEAN         NOT NULL DEFAULT TRUE,
    endpoint     VARCHAR(200),
    PRIMARY KEY (event_id)
);

CREATE INDEX IF NOT EXISTS idx_security_ip_ts ON security_events (client_ip, ts DESC);

CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    tool            VARCHAR(30)     NOT NULL,
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

-- 6. Continuous Aggregates
-- ohlcv_1m
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'ohlcv_1m') THEN
        CREATE MATERIALIZED VIEW ohlcv_1m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 minute', ts) AS bucket, symbol, FIRST(price, ts) AS open, MAX(price) AS high, MIN(price) AS low, LAST(price, ts) AS close, SUM(volume) AS volume
        FROM raw_ticks GROUP BY bucket, symbol WITH NO DATA;
    END IF;
END $$;

-- ohlcv_5m
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'ohlcv_5m') THEN
        CREATE MATERIALIZED VIEW ohlcv_5m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('5 minutes', ts) AS bucket, symbol, FIRST(price, ts) AS open, MAX(price) AS high, MIN(price) AS low, LAST(price, ts) AS close, SUM(volume) AS volume
        FROM raw_ticks GROUP BY bucket, symbol WITH NO DATA;
    END IF;
END $$;

-- ohlcv_15m
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'ohlcv_15m') THEN
        CREATE MATERIALIZED VIEW ohlcv_15m
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('15 minutes', ts) AS bucket, symbol, FIRST(price, ts) AS open, MAX(price) AS high, MIN(price) AS low, LAST(price, ts) AS close, SUM(volume) AS volume
        FROM raw_ticks GROUP BY bucket, symbol WITH NO DATA;
    END IF;
END $$;

-- ohlcv_1h
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name = 'ohlcv_1h') THEN
        CREATE MATERIALIZED VIEW ohlcv_1h
        WITH (timescaledb.continuous) AS
        SELECT time_bucket('1 hour', ts) AS bucket, symbol, FIRST(price, ts) AS open, MAX(price) AS high, MIN(price) AS low, LAST(price, ts) AS close, SUM(volume) AS volume
        FROM raw_ticks GROUP BY bucket, symbol WITH NO DATA;
    END IF;
END $$;

-- 7. Policies
SELECT add_continuous_aggregate_policy('ohlcv_1m',
    start_offset => INTERVAL '10 minutes', end_offset => INTERVAL '1 minute', schedule_interval => INTERVAL '1 minute', if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('ohlcv_5m',
    start_offset => INTERVAL '50 minutes', end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('ohlcv_15m',
    start_offset => INTERVAL '150 minutes', end_offset => INTERVAL '15 minutes', schedule_interval => INTERVAL '15 minutes', if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('ohlcv_1h',
    start_offset => INTERVAL '10 hours', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);

-- Compression policy for raw_ticks
DO $$
BEGIN
    PERFORM set_chunk_time_interval('raw_ticks', INTERVAL '1 day');
EXCEPTION
    WHEN OTHERS THEN NULL;
END $$;

ALTER TABLE raw_ticks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'ts DESC'
);
SELECT add_compression_policy('raw_ticks', INTERVAL '7 days', if_not_exists => TRUE);

-- Retention policy for raw_ticks
SELECT add_retention_policy('raw_ticks', INTERVAL '90 days', if_not_exists => TRUE);
