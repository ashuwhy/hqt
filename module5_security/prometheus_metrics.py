"""
Prometheus Metrics Registry for HQT Security Proxy.

All counters, histograms, and gauges that the security proxy tracks
are defined here and imported by main.py.
"""
from prometheus_client import Counter, Gauge, Histogram

# ── LOB metrics (mirrored from lob-engine via proxy) ──────────────────────────
lob_orders_total = Counter(
    "lob_orders_total",
    "Total limit/market orders submitted",
    ["symbol", "side"],
)
lob_trades_total = Counter(
    "lob_trades_total",
    "Total matched trades",
    ["symbol"],
)
lob_order_latency_ms = Histogram(
    "lob_order_latency_ms",
    "Order submission latency (ms) as observed by the proxy",
    buckets=[0.5, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000],
)
lob_active_orders = Gauge(
    "lob_active_orders",
    "Current number of active orders in the LOB",
    ["symbol"],
)

# ── Security metrics ──────────────────────────────────────────────────────────
sql_injections_total = Counter(
    "security_sql_injections_total",
    "SQL injection attempts detected and blocked",
)
rate_limit_hits_total = Counter(
    "security_rate_limit_total",
    "Requests rejected by the rate limiter",
    ["client_ip"],
)
proxy_requests_total = Counter(
    "security_proxy_requests_total",
    "Total requests proxied to upstream services",
    ["upstream", "method", "status"],
)
proxy_latency_ms = Histogram(
    "security_proxy_latency_ms",
    "Latency of upstream proxy calls (ms)",
    ["upstream"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000],
)

# ── Quantum / arbitrage metrics ───────────────────────────────────────────────
arbitrage_signals_total = Counter(
    "quantum_arbitrage_signals_total",
    "Arbitrage signals detected",
    ["method"],
)
