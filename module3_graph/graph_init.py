"""
module3_graph.graph_init
~~~~~~~~~~~~~~~~~~~~~~~~
Idempotent initialisation of the Apache AGE FX graph.

* MERGE 20 Asset nodes  (10 crypto + 10 fiat)
* MERGE directed EXCHANGE edges — seeded with **real** rates from:
    - Kraken REST API               (crypto → USD pairs)
    - Alpha Vantage FX API          (fiat real-time bid/ask)
    - Calculated cross-rates        (crypto↔crypto, crypto↔fiat via USD)
* Safe to re-run at any time — uses MERGE, never duplicates.

Usage (standalone):
    python -m module3_graph.graph_init
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import psycopg
import requests

logger = logging.getLogger(__name__)

# ── Asset lists ──────────────────────────────────────────────────────────────
CRYPTO = ["BTC", "ETH", "LINK", "SOL", "ADA", "XRP", "DOGE", "AVAX", "UNI", "DOT"]
FIAT = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "INR", "SGD", "HKD"]
ALL_ASSETS = CRYPTO + FIAT

# Kraken ticker symbols → our internal symbol.  Only crypto/USD pairs.
# See https://docs.kraken.com/api/docs/rest-api/get-ticker-information
KRAKEN_TICKER_MAP = {
    "XXBTZUSD": "BTC",
    "XETHZUSD": "ETH",
    "LINKUSD":  "LINK",
    "SOLUSD":   "SOL",
    "ADAUSD":   "ADA",
    "XXRPZUSD": "XRP",
    "XDGUSD":   "DOGE",
    "AVAXUSD":  "AVAX",
    "UNIUSD":   "UNI",
    "DOTUSD":   "DOT",
}

# Alpha Vantage API key from .env
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_API_KEY", "")
FIAT_TARGETS = [f for f in FIAT if f != "USD"]


# ── Real-rate fetchers ───────────────────────────────────────────────────────

def _fetch_kraken_prices() -> dict[str, tuple[float, float]]:
    """Fetch real bid/ask for all crypto/USD pairs from Kraken public API.

    Returns: {symbol: (bid, ask)}   e.g. {"BTC": (67123.4, 67135.2)}
    """
    pairs = ",".join(KRAKEN_TICKER_MAP.keys())
    url = f"https://api.kraken.com/0/public/Ticker?pair={pairs}"
    logger.info("Fetching real crypto prices from Kraken…")

    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            logger.warning("Kraken API error: %s", data["error"])
            return {}

        result: dict[str, tuple[float, float]] = {}
        for kraken_pair, symbol in KRAKEN_TICKER_MAP.items():
            ticker = data["result"].get(kraken_pair)
            if ticker:
                bid = float(ticker["b"][0])   # best bid [price, whole_lot, lot_vol]
                ask = float(ticker["a"][0])   # best ask
                result[symbol] = (bid, ask)
                logger.info("  %s/USD  bid=%.6f  ask=%.6f", symbol, bid, ask)
        return result
    except Exception as exc:
        logger.warning("Kraken fetch failed: %s — will use fallback rates", exc)
        return {}


def _fetch_single_fx(from_ccy: str, to_ccy: str) -> tuple[str, float, float, float] | None:
    """Fetch a single FX pair from Alpha Vantage CURRENCY_EXCHANGE_RATE API.

    Returns: (to_ccy, bid, ask, rate) or None on failure.
    Alpha Vantage free tier: 25 req/day.  We fetch 9 fiat pairs at init.
    """
    url = (
        f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
        f"&from_currency={from_ccy}&to_currency={to_ccy}"
        f"&apikey={ALPHA_VANTAGE_KEY}"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        info = data.get("Realtime Currency Exchange Rate", {})
        if not info:
            return None
        rate = float(info.get("5. Exchange Rate", 0))
        bid = float(info.get("8. Bid Price", rate))
        ask = float(info.get("9. Ask Price", rate))
        return (to_ccy, bid, ask, rate)
    except Exception as exc:
        logger.debug("Alpha Vantage %s→%s failed: %s", from_ccy, to_ccy, exc)
        return None


def _fetch_fiat_rates() -> dict[str, dict[str, float]]:
    """Fetch real-time fiat exchange rates from Alpha Vantage.

    Returns: {currency: {"bid": ..., "ask": ..., "rate": ...}}
    Uses ALPHA_VANTAGE_API_KEY from .env.
    Falls back to Frankfurter/ECB if Alpha Vantage fails.
    """
    result: dict[str, dict[str, float]] = {}

    if ALPHA_VANTAGE_KEY:
        logger.info("Fetching real fiat rates from Alpha Vantage (key=%s…)…", ALPHA_VANTAGE_KEY[:6])
        # Fetch pairs sequentially (Alpha Vantage rate-limits concurrent)
        for ccy in FIAT_TARGETS:
            data = _fetch_single_fx("USD", ccy)
            if data:
                _, bid, ask, rate = data
                result[ccy] = {"bid": bid, "ask": ask, "rate": rate}
                logger.info("  USD/%s  bid=%.6f  ask=%.6f  mid=%.6f", ccy, bid, ask, rate)
                time.sleep(0.5)  # Respect rate limit
    else:
        logger.warning("ALPHA_VANTAGE_API_KEY not set, trying Frankfurter/ECB…")

    # Fallback to Frankfurter if Alpha Vantage returned nothing
    if not result:
        logger.info("Falling back to Frankfurter/ECB for fiat rates…")
        targets = ",".join(FIAT_TARGETS)
        url = f"https://api.frankfurter.app/latest?from=USD&to={targets}"
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            for ccy, rate in data.get("rates", {}).items():
                spread = rate * 0.0001
                result[ccy] = {"bid": rate - spread/2, "ask": rate + spread/2, "rate": rate}
                logger.info("  USD/%s = %.6f (ECB mid)", ccy, rate)
        except Exception as exc:
            logger.warning("Frankfurter also failed: %s — using fallbacks", exc)

    return result


# ── Fallback rates (approximate, only used if APIs are unreachable) ──────────
FALLBACK_CRYPTO_USD: dict[str, tuple[float, float]] = {
    "BTC": (67000.0, 67050.0), "ETH": (3480.0, 3482.0),
    "LINK": (14.50, 14.52),    "SOL": (145.0, 145.20),
    "ADA": (0.62, 0.621),      "XRP": (0.58, 0.581),
    "DOGE": (0.165, 0.1655),   "AVAX": (38.0, 38.10),
    "UNI": (6.50, 6.52),       "DOT": (7.80, 7.82),
}
FALLBACK_FIAT: dict[str, dict[str, float]] = {
    "EUR": {"bid": 0.9212, "ask": 0.9214, "rate": 0.9213},
    "GBP": {"bid": 0.7904, "ask": 0.7906, "rate": 0.7905},
    "JPY": {"bid": 149.49, "ask": 149.51, "rate": 149.50},
    "AUD": {"bid": 1.5266, "ask": 1.5268, "rate": 1.5267},
    "CAD": {"bid": 1.3579, "ask": 1.3581, "rate": 1.3580},
    "CHF": {"bid": 0.8819, "ask": 0.8821, "rate": 0.8820},
    "INR": {"bid": 83.09,  "ask": 83.11,  "rate": 83.10},
    "SGD": {"bid": 1.3419, "ask": 1.3421, "rate": 1.3420},
    "HKD": {"bid": 7.8249, "ask": 7.8251, "rate": 7.8250},
}


def _build_all_pairs(
    crypto_prices: dict[str, tuple[float, float]],
    fiat_rates: dict[str, dict[str, float]],
) -> list[tuple[str, str, float, float]]:
    """Build the full list of (src, dst, bid, ask) pairs from real data.

    Categories:
        1. Crypto → USD  (direct from Kraken)
        2. USD → Crypto  (inverse)
        3. Crypto ↔ Crypto cross-rates  (via USD)
        4. USD → Fiat  (real bid/ask from Alpha Vantage)
        5. Fiat → USD  (inverse)
        6. Fiat ↔ Fiat cross-rates  (via USD)
        7. Crypto → Fiat  (via USD)
        8. Fiat → Crypto  (via USD)
    """
    pairs: list[tuple[str, str, float, float]] = []

    # Use real data or fall back gracefully
    cp = crypto_prices if crypto_prices else FALLBACK_CRYPTO_USD
    fx = fiat_rates if fiat_rates else FALLBACK_FIAT

    # ── 1 & 2: Crypto ↔ USD ─────────────────────────────────────────────────
    for sym, (bid, ask) in cp.items():
        pairs.append((sym, "USD", bid, ask))
        # Inverse: USD → Crypto
        inv_bid = 1.0 / ask
        inv_ask = 1.0 / bid
        pairs.append(("USD", sym, inv_bid, inv_ask))

    # ── 3: Crypto ↔ Crypto cross-rates (through USD) ────────────────────────
    crypto_list = list(cp.keys())
    for i, c1 in enumerate(crypto_list):
        for j, c2 in enumerate(crypto_list):
            if i == j:
                continue
            c1_bid, _ = cp[c1]
            _, c2_ask = cp[c2]
            if c2_ask > 0:
                cross_bid = c1_bid / c2_ask
                cross_ask = cp[c1][1] / cp[c2][0] if cp[c2][0] > 0 else cross_bid * 1.001
                pairs.append((c1, c2, cross_bid, cross_ask))

    # ── 4 & 5: Fiat ↔ USD (real bid/ask from Alpha Vantage) ─────────────────
    for ccy, info in fx.items():
        bid = info["bid"]
        ask = info["ask"]
        if bid <= 0 or ask <= 0:
            continue
        pairs.append(("USD", ccy, bid, ask))
        # Fiat → USD = inverse
        pairs.append((ccy, "USD", 1.0 / ask, 1.0 / bid))

    # ── 6: Fiat ↔ Fiat cross-rates (through USD) ────────────────────────────
    fiat_list = list(fx.keys())
    for f1 in fiat_list:
        for f2 in fiat_list:
            if f1 == f2:
                continue
            r1 = fx[f1]["rate"]
            r2 = fx[f2]["rate"]
            if r1 > 0 and r2 > 0:
                cross = r2 / r1
                spread = cross * 0.0002
                pairs.append((f1, f2, cross - spread / 2, cross + spread / 2))

    # ── 7 & 8: Crypto ↔ Fiat (through USD) ──────────────────────────────────
    for sym, (c_bid, c_ask) in cp.items():
        for ccy, info in fx.items():
            fiat_rate = info["rate"]
            if fiat_rate <= 0:
                continue
            c2f_bid = c_bid * fiat_rate
            c2f_ask = c_ask * fiat_rate
            pairs.append((sym, ccy, c2f_bid, c2f_ask))
            if c2f_ask > 0 and c2f_bid > 0:
                pairs.append((ccy, sym, 1.0 / c2f_ask, 1.0 / c2f_bid))

    logger.info("Built %d exchange pairs from real data", len(pairs))
    return pairs


# ── Database helpers ─────────────────────────────────────────────────────────

def _dsn() -> str:
    """Build a PostgreSQL DSN from environment variables."""
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'hqt')} "
        f"user={os.getenv('POSTGRES_USER', 'hqt')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    )


def _age_cypher(conn: psycopg.Connection, cypher: str) -> list[tuple]:
    """Execute a Cypher query through AGE's SQL wrapper."""
    sql = f"SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ {cypher} $cypher$) AS (v agtype);"
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        cur.execute(sql)
        return cur.fetchall()


def _merge_nodes(conn: psycopg.Connection) -> int:
    """MERGE all Asset nodes. Returns count of nodes after operation."""
    for symbol in ALL_ASSETS:
        asset_type = "crypto" if symbol in CRYPTO else "fiat"
        cypher = f"MERGE (a:Asset {{symbol: '{symbol}', asset_type: '{asset_type}'}})"
        _age_cypher(conn, cypher)
    conn.commit()

    rows = _age_cypher(conn, "MATCH (a:Asset) RETURN count(a)")
    count = int(str(rows[0][0])) if rows else 0
    logger.info("Asset nodes in graph: %d", count)
    return count


def _merge_edges(conn: psycopg.Connection,
                 pairs: list[tuple[str, str, float, float]]) -> int:
    """MERGE all EXCHANGE edges with real rates. Returns edge count."""
    ts = int(time.time() * 1000)
    for src, dst, bid, ask in pairs:
        spread = ask - bid
        cypher = (
            f"MATCH (a:Asset {{symbol: '{src}'}}), (b:Asset {{symbol: '{dst}'}}) "
            f"MERGE (a)-[r:EXCHANGE]->(b) "
            f"SET r.bid = {bid}, r.ask = {ask}, r.spread = {spread}, r.last_updated = {ts}"
        )
        _age_cypher(conn, cypher)
    conn.commit()

    rows = _age_cypher(conn, "MATCH ()-[r:EXCHANGE]->() RETURN count(r)")
    count = int(str(rows[0][0])) if rows else 0
    logger.info("EXCHANGE edges in graph: %d", count)
    return count


def init_graph(conn: psycopg.Connection | None = None) -> dict:
    """Initialise the AGE fx_graph with real market data.  Idempotent.

    1. Fetch real crypto prices from Kraken REST API
    2. Fetch real fiat rates from Frankfurter/ECB API
    3. MERGE 20 Asset nodes
    4. MERGE all EXCHANGE edges with real bid/ask

    Returns dict with ``node_count`` and ``edge_count``.
    """
    own_conn = False
    if conn is None:
        conn = psycopg.connect(_dsn(), autocommit=False)
        own_conn = True

    try:
        # 1. Fetch real rates
        crypto_prices = _fetch_kraken_prices()
        fiat_rates = _fetch_fiat_rates()

        # 2. Build all pairs
        all_pairs = _build_all_pairs(crypto_prices, fiat_rates)

        # 3. MERGE nodes
        node_count = _merge_nodes(conn)

        # 4. MERGE edges with real rates
        edge_count = _merge_edges(conn, all_pairs)

        return {"node_count": node_count, "edge_count": edge_count}
    finally:
        if own_conn:
            conn.close()


# ── CLI entry-point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = init_graph()
    print(f"Graph initialised: {result}")
