-- ============================================================================
-- TimescaleDB SQL Indicator Functions
-- Run: psql -U hqt -d hqt -f indicators.sql
-- ============================================================================

-- 1. VWAP — Volume-Weighted Average Price
-- Usage: SELECT fn_vwap('BTC/USD', '2026-03-01', '2026-03-11');
CREATE OR REPLACE FUNCTION fn_vwap(
    p_symbol  VARCHAR,
    p_from    TIMESTAMPTZ,
    p_to      TIMESTAMPTZ
)
RETURNS NUMERIC AS $$
    SELECT COALESCE(
        SUM(price * volume) / NULLIF(SUM(volume), 0),
        0
    )
    FROM raw_ticks
    WHERE symbol = p_symbol
      AND ts >= p_from
      AND ts <  p_to;
$$ LANGUAGE sql STABLE;


-- 2. SMA-20 — Simple Moving Average of the last 20 one-minute closes
-- Usage: SELECT fn_sma20('BTC/USD', NOW());
CREATE OR REPLACE FUNCTION fn_sma20(
    p_symbol  VARCHAR,
    p_at      TIMESTAMPTZ
)
RETURNS NUMERIC AS $$
    SELECT AVG(close)
    FROM (
        SELECT close
        FROM ohlcv_1m
        WHERE symbol = p_symbol
          AND bucket <= p_at
        ORDER BY bucket DESC
        LIMIT 20
    ) sub;
$$ LANGUAGE sql STABLE;


-- 3. Bollinger Bands — (sma20, upper = sma20 + 2σ, lower = sma20 − 2σ)
-- Returns a record with three fields
-- Usage: SELECT * FROM fn_bollinger('BTC/USD', NOW());
DROP TYPE IF EXISTS bollinger_result CASCADE;
CREATE TYPE bollinger_result AS (
    sma20  NUMERIC,
    upper  NUMERIC,
    lower  NUMERIC
);

CREATE OR REPLACE FUNCTION fn_bollinger(
    p_symbol  VARCHAR,
    p_at      TIMESTAMPTZ
)
RETURNS bollinger_result AS $$
    SELECT
        AVG(close)::NUMERIC                              AS sma20,
        (AVG(close) + 2 * STDDEV_SAMP(close))::NUMERIC  AS upper,
        (AVG(close) - 2 * STDDEV_SAMP(close))::NUMERIC  AS lower
    FROM (
        SELECT close
        FROM ohlcv_1m
        WHERE symbol = p_symbol
          AND bucket <= p_at
        ORDER BY bucket DESC
        LIMIT 20
    ) sub;
$$ LANGUAGE sql STABLE;


-- 4. RSI-14 — Relative Strength Index over last 14 one-minute periods
-- Uses the smoothed (Wilder) method: RSI = 100 - 100/(1 + avg_gain/avg_loss)
-- Usage: SELECT fn_rsi14('BTC/USD', NOW());
CREATE OR REPLACE FUNCTION fn_rsi14(
    p_symbol  VARCHAR,
    p_at      TIMESTAMPTZ
)
RETURNS NUMERIC AS $$
    WITH recent AS (
        SELECT close, LAG(close) OVER (ORDER BY bucket) AS prev_close
        FROM ohlcv_1m
        WHERE symbol = p_symbol
          AND bucket <= p_at
        ORDER BY bucket DESC
        LIMIT 15  -- need 15 rows to get 14 deltas
    ),
    deltas AS (
        SELECT
            GREATEST(close - prev_close, 0) AS gain,
            GREATEST(prev_close - close, 0) AS loss
        FROM recent
        WHERE prev_close IS NOT NULL
    ),
    averages AS (
        SELECT
            AVG(gain) AS avg_gain,
            AVG(loss) AS avg_loss
        FROM deltas
    )
    SELECT
        CASE
            WHEN avg_loss = 0 THEN 100
            ELSE ROUND(100 - 100.0 / (1 + avg_gain / avg_loss), 4)
        END
    FROM averages;
$$ LANGUAGE sql STABLE;
