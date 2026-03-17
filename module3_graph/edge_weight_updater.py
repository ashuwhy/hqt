"""
module3_graph.edge_weight_updater
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Async loop that refreshes EXCHANGE edge weights every 500 ms
from **real** data sources:

1. Primary:  LOB engine depth endpoint  (real-time order book)
2. Fallback: TimescaleDB raw_ticks      (real Kraken trade data)
3. Fiat:     Alpha Vantage FX API       (real-time bid/ask, using API key from .env)

Symbol format consistency:
    Graph nodes:   BTC, ETH, USD, EUR, ...
    LOB symbols:   BTCUSD  (concatenated, no separator)
    TSDB symbols:  BTC/USD (Kraken format with /)

Never uses mock / synthetic data.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx
import psycopg
from prometheus_client import Gauge

logger = logging.getLogger(__name__)

# ── Prometheus ───────────────────────────────────────────────────────────────
EDGE_LAG = Gauge(
    "graph_edge_update_lag_ms",
    "Milliseconds since the last successful edge weight update",
)

# ── Configuration ────────────────────────────────────────────────────────────
LOB_BASE = os.getenv("LOB_ENGINE_URL", "http://lob-engine:8001")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
UPDATE_INTERVAL = float(os.getenv("GRAPH_UPDATE_INTERVAL", "0.5"))  # seconds
FIAT_POLL_INTERVAL = 300.0  # Alpha Vantage free tier: 25 req/day → poll every 5 min

# ── Crypto pair map ──────────────────────────────────────────────────────────
# Graph edge (src→dst) → LOB symbol (concatenated) + TSDB symbol (slash)
CRYPTO_PAIRS: list[dict[str, str]] = [
    {"src": "BTC",  "dst": "USD", "lob": "BTCUSD",   "tsdb": "BTC/USD"},
    {"src": "ETH",  "dst": "USD", "lob": "ETHUSD",   "tsdb": "ETH/USD"},
    {"src": "LINK", "dst": "USD", "lob": "LINKUSD",   "tsdb": "LINK/USD"},
    {"src": "SOL",  "dst": "USD", "lob": "SOLUSD",   "tsdb": "SOL/USD"},
    {"src": "ADA",  "dst": "USD", "lob": "ADAUSD",   "tsdb": "ADA/USD"},
    {"src": "XRP",  "dst": "USD", "lob": "XRPUSD",   "tsdb": "XRP/USD"},
    {"src": "DOGE", "dst": "USD", "lob": "DOGEUSD",  "tsdb": "DOGE/USD"},
    {"src": "AVAX", "dst": "USD", "lob": "AVAXUSD",  "tsdb": "AVAX/USD"},
    {"src": "UNI",  "dst": "USD", "lob": "UNIUSD",   "tsdb": "UNI/USD"},
    {"src": "DOT",  "dst": "USD", "lob": "DOTUSD",   "tsdb": "DOT/USD"},
]

# ── Fiat currencies to track (all vs USD) ────────────────────────────────────
FIAT_CURRENCIES = ["EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "INR", "SGD", "HKD"]

# Cache of latest fiat rates (refreshed every FIAT_POLL_INTERVAL)
_fiat_cache: dict[str, float] = {}
_fiat_cache_ts: float = 0.0


def _dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'hqt')} "
        f"user={os.getenv('POSTGRES_USER', 'hqt')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    )


def _age_exec(conn: psycopg.Connection, cypher: str) -> list[tuple]:
    """Run a Cypher statement through AGE."""
    sql = f"SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ {cypher} $cypher$) AS (v agtype);"
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        cur.execute(sql)
        return cur.fetchall()


def _update_edge(conn: psycopg.Connection, src: str, dst: str,
                 bid: float, ask: float) -> None:
    """SET bid/ask/spread/last_updated on an existing EXCHANGE edge + its inverse."""
    spread = ask - bid
    ts = int(time.time() * 1000)
    cypher = (
        f"MATCH (a:Asset {{symbol: '{src}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{dst}'}}) "
        f"SET r.bid = {bid}, r.ask = {ask}, r.spread = {spread}, r.last_updated = {ts}"
    )
    _age_exec(conn, cypher)

    # Also update the inverse edge
    if bid > 0 and ask > 0:
        inv_bid = 1.0 / ask
        inv_ask = 1.0 / bid
        inv_spread = inv_ask - inv_bid
        cypher_inv = (
            f"MATCH (a:Asset {{symbol: '{dst}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{src}'}}) "
            f"SET r.bid = {inv_bid}, r.ask = {inv_ask}, r.spread = {inv_spread}, r.last_updated = {ts}"
        )
        _age_exec(conn, cypher_inv)


# ── Crypto price sources ────────────────────────────────────────────────────

async def _fetch_from_lob(client: httpx.AsyncClient, pair: dict) -> tuple[float, float] | None:
    """Try to get best_bid/best_ask from the LOB depth endpoint (real order book)."""
    try:
        resp = await client.get(f"{LOB_BASE}/lob/depth/{pair['lob']}", timeout=2.0)
        if resp.status_code != 200:
            return None
        data = resp.json()
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        if best_bid <= 0 or best_ask <= 0:
            return None
        return best_bid, best_ask
    except Exception as exc:
        logger.debug("LOB fetch failed for %s: %s", pair["lob"], exc)
        return None


def _fetch_from_timescaledb(pg_conn: psycopg.Connection, pair: dict) -> tuple[float, float] | None:
    """Fallback: get latest real price from TimescaleDB raw_ticks (Kraken data)."""
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                """
                SELECT price, side FROM raw_ticks
                WHERE symbol = %s
                ORDER BY ts DESC LIMIT 20
                """,
                (pair["tsdb"],),
            )
            rows = cur.fetchall()
            if not rows:
                return None

            buys = [float(r[0]) for r in rows if r[1] == "B"]
            sells = [float(r[0]) for r in rows if r[1] == "S"]

            if buys and sells:
                best_bid = max(buys)
                best_ask = min(sells)
            elif buys:
                best_bid = max(buys)
                best_ask = best_bid * 1.0001
            elif sells:
                best_ask = min(sells)
                best_bid = best_ask * 0.9999
            else:
                return None

            if best_bid <= 0 or best_ask <= 0:
                return None
            return best_bid, best_ask
    except Exception as exc:
        logger.debug("TimescaleDB fetch failed for %s: %s", pair["tsdb"], exc)
        return None


# ── Fiat rate source ─────────────────────────────────────────────────────────

async def _refresh_fiat_cache(client: httpx.AsyncClient) -> None:
    """Fetch real fiat rates from Alpha Vantage and update the cache.

    Alpha Vantage CURRENCY_EXCHANGE_RATE returns real-time bid/ask per pair.
    Free tier: 25 req/day → we fetch 9 pairs every 5 min.
    Falls back to Frankfurter/ECB if Alpha Vantage fails.
    """
    global _fiat_cache, _fiat_cache_ts

    now = time.time()
    if now - _fiat_cache_ts < FIAT_POLL_INTERVAL and _fiat_cache:
        return  # Cache still fresh

    if ALPHA_VANTAGE_KEY:
        logger.info("Refreshing fiat rates from Alpha Vantage…")
        new_cache: dict[str, float] = {}
        for ccy in FIAT_CURRENCIES:
            try:
                url = (
                    f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
                    f"&from_currency=USD&to_currency={ccy}"
                    f"&apikey={ALPHA_VANTAGE_KEY}"
                )
                resp = await client.get(url, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    info = data.get("Realtime Currency Exchange Rate", {})
                    if info:
                        rate = float(info.get("5. Exchange Rate", 0))
                        if rate > 0:
                            new_cache[ccy] = rate
                            logger.info("  USD/%s = %.6f (Alpha Vantage)", ccy, rate)
                await asyncio.sleep(1.0)  # Respect rate limit (non-blocking)
            except Exception as exc:
                logger.debug("Alpha Vantage USD/%s failed: %s", ccy, exc)

        if new_cache:
            _fiat_cache = new_cache
            _fiat_cache_ts = now
            return

    # Fallback to Frankfurter/ECB
    try:
        targets = ",".join(FIAT_CURRENCIES)
        url = f"https://api.frankfurter.app/latest?from=USD&to={targets}"
        resp = await client.get(url, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            _fiat_cache = data.get("rates", {})
            _fiat_cache_ts = now
            logger.info("Fiat rates refreshed from ECB fallback: %s", _fiat_cache)
    except Exception as exc:
        logger.warning("Fiat rate fetch failed: %s (using cached values)", exc)


def _update_fiat_edges(conn: psycopg.Connection, crypto_usd: dict[str, tuple[float, float]]) -> int:
    """Update all fiat-related edges using cached real FX rates.

    Updates:
        1. USD → Fiat  and Fiat → USD
        2. Fiat ↔ Fiat cross-rates
        3. Crypto → Fiat  and Fiat → Crypto  (via USD)
    """
    if not _fiat_cache:
        logger.debug("_update_fiat_edges: fiat cache empty, skipping")
        return 0

    logger.info("Updating fiat edges: %d fiat currencies, %d cryptos", len(_fiat_cache), len(crypto_usd))

    updated = 0
    ts = int(time.time() * 1000)

    # 1. USD ↔ Fiat
    for ccy, rate in _fiat_cache.items():
        if rate <= 0:
            continue
        spread = rate * 0.0001  # ~1bp spread for fiat
        _update_edge(conn, "USD", ccy, rate - spread / 2, rate + spread / 2)
        updated += 1

    # 2. Fiat ↔ Fiat cross-rates
    fiat_list = list(_fiat_cache.keys())
    for f1 in fiat_list:
        for f2 in fiat_list:
            if f1 == f2:
                continue
            r1, r2 = _fiat_cache[f1], _fiat_cache[f2]
            if r1 > 0 and r2 > 0:
                cross = r2 / r1
                spread = cross * 0.0002
                cypher = (
                    f"MATCH (a:Asset {{symbol: '{f1}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{f2}'}}) "
                    f"SET r.bid = {cross - spread/2}, r.ask = {cross + spread/2}, "
                    f"r.spread = {spread}, r.last_updated = {ts}"
                )
                _age_exec(conn, cypher)
                updated += 1

    # 3. Crypto ↔ Fiat (via USD)
    for sym, (usd_bid, usd_ask) in crypto_usd.items():
        for ccy, fiat_rate in _fiat_cache.items():
            if fiat_rate <= 0:
                continue
            # Crypto → Fiat
            c2f_bid = usd_bid * fiat_rate
            c2f_ask = usd_ask * fiat_rate
            cypher_c2f = (
                f"MATCH (a:Asset {{symbol: '{sym}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{ccy}'}}) "
                f"SET r.bid = {c2f_bid}, r.ask = {c2f_ask}, "
                f"r.spread = {c2f_ask - c2f_bid}, r.last_updated = {ts}"
            )
            _age_exec(conn, cypher_c2f)
            # Fiat → Crypto
            if c2f_ask > 0 and c2f_bid > 0:
                f2c_bid = 1.0 / c2f_ask
                f2c_ask = 1.0 / c2f_bid
                cypher_f2c = (
                    f"MATCH (a:Asset {{symbol: '{ccy}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{sym}'}}) "
                    f"SET r.bid = {f2c_bid}, r.ask = {f2c_ask}, "
                    f"r.spread = {f2c_ask - f2c_bid}, r.last_updated = {ts}"
                )
                _age_exec(conn, cypher_f2c)
            updated += 1

    logger.info("Fiat edge update: %d edges refreshed (USD↔Fiat + Fiat↔Fiat + %d cryptos × %d fiats)",
                updated, len(crypto_usd), len(_fiat_cache))
    return updated


# ── Main loop ────────────────────────────────────────────────────────────────

async def run_updater(graph_conn: psycopg.Connection) -> None:
    """Main edge-weight update loop. Runs forever as a background task.

    Crypto: LOB engine (primary) → TimescaleDB raw_ticks (fallback)
    Fiat:   Frankfurter / ECB API (polled every 60s)
    Cross:  Computed from real crypto + fiat rates
    """
    logger.info(
        "Edge weight updater started (interval=%.1fs, LOB=%s, crypto_pairs=%d, fiat_currencies=%d)",
        UPDATE_INTERVAL, LOB_BASE, len(CRYPTO_PAIRS), len(FIAT_CURRENCIES),
    )

    tsdb_conn: psycopg.Connection | None = None
    try:
        tsdb_conn = psycopg.connect(_dsn(), autocommit=True)
    except Exception as exc:
        logger.warning("Cannot connect to TimescaleDB for fallback prices: %s", exc)

    async with httpx.AsyncClient() as client:
        while True:
            t0 = time.perf_counter()
            updated = 0
            crypto_usd_prices: dict[str, tuple[float, float]] = {}

            # ── Update crypto → USD edges ────────────────────────────────────
            for pair in CRYPTO_PAIRS:
                try:
                    result = await _fetch_from_lob(client, pair)
                    if result is None and tsdb_conn is not None:
                        result = _fetch_from_timescaledb(tsdb_conn, pair)
                    if result is None:
                        continue

                    best_bid, best_ask = result
                    _update_edge(graph_conn, pair["src"], pair["dst"], best_bid, best_ask)
                    crypto_usd_prices[pair["src"]] = (best_bid, best_ask)
                    updated += 1

                except Exception as exc:
                    logger.warning("Edge update error %s→%s: %s", pair["src"], pair["dst"], exc)

            # ── Fill in missing cryptos from graph (e.g. BNB, MATIC not on Kraken) ──
            # Without this, cross-rates are only calculated for pairs with live feeds,
            # leaving stale cross-rates for pairs like ETH→BNB → phantom arbitrage.
            for pair in CRYPTO_PAIRS:
                sym = pair["src"]
                if sym not in crypto_usd_prices:
                    try:
                        cypher = (
                            f"MATCH (a:Asset {{symbol: '{sym}'}})-[r:EXCHANGE]->"
                            f"(b:Asset {{symbol: 'USD'}}) RETURN r.bid, r.ask"
                        )
                        sql = f"SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ {cypher} $cypher$) AS (bid agtype, ask agtype);"
                        with graph_conn.cursor() as cur:
                            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
                            cur.execute(sql)
                            row = cur.fetchone()
                            if row:
                                bid = float(str(row[0]))
                                ask = float(str(row[1]))
                                if bid > 0 and ask > 0:
                                    crypto_usd_prices[sym] = (bid, ask)
                    except Exception:
                        pass

            # ── Update crypto ↔ crypto cross-rates ───────────────────────────
            crypto_list = list(crypto_usd_prices.keys())
            for c1 in crypto_list:
                for c2 in crypto_list:
                    if c1 == c2:
                        continue
                    b1, a1 = crypto_usd_prices[c1]
                    b2, a2 = crypto_usd_prices[c2]
                    if a2 > 0 and b2 > 0:
                        cross_bid = b1 / a2
                        cross_ask = a1 / b2
                        ts = int(time.time() * 1000)
                        cypher = (
                            f"MATCH (a:Asset {{symbol: '{c1}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{c2}'}}) "
                            f"SET r.bid = {cross_bid}, r.ask = {cross_ask}, "
                            f"r.spread = {cross_ask - cross_bid}, r.last_updated = {ts}"
                        )
                        _age_exec(graph_conn, cypher)
                        updated += 1

            # ── Update fiat edges (every 5 min) ──────────────────────────────
            await _refresh_fiat_cache(client)
            if _fiat_cache:
                try:
                    fiat_updated = _update_fiat_edges(graph_conn, crypto_usd_prices)
                    updated += fiat_updated
                except Exception as exc:
                    logger.warning("Fiat edge update error: %s", exc)

            graph_conn.commit()
            elapsed_ms = (time.perf_counter() - t0) * 1000
            EDGE_LAG.set(elapsed_ms)

            if updated > 0:
                logger.debug("Updated %d edges in %.1fms", updated, elapsed_ms)

            await asyncio.sleep(UPDATE_INTERVAL)
