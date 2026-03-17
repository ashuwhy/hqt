"""
HQT Security Proxy — Central API gateway.

Routes traffic to backend microservices:
    /lob/*       → lob-engine:8001
    /graph/*     → graph-service:8003
    /analytics/* → data-ingestor:8002
    /quantum/*   → quantum-engine:8004

Also hosts /health, /metrics, and /admin/* endpoints directly.
"""

from __future__ import annotations

import logging
import os

import asyncpg
import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

# Importing prometheus_metrics registers all metrics with the global registry.
import module5_security.prometheus_metrics  # noqa: F401

from module5_security.rate_limiter import init_redis, rate_limit_middleware
from module5_security.sql_firewall import sql_firewall_middleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("security_proxy")

app = FastAPI(title="HQT Security Proxy")

# ── Upstream service URLs ────────────────────────────────────────────────────
LOB_URL = os.getenv("LOB_ENGINE_URL", "http://lob-engine:8001")
GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")
QUANTUM_URL = os.getenv("QUANTUM_ENGINE_URL", "http://quantum-engine:8004")
ANALYTICS_URL = os.getenv("ANALYTICS_URL", "http://data-ingestor:8002")

# ── Database DSN ─────────────────────────────────────────────────────────────
PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# ── Middleware ────────────────────────────────────────────────────────────────
# In Starlette, the last-registered middleware is outermost (executes first).
# So sql_firewall (registered second) runs before rate_limit (registered first).


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    return await rate_limit_middleware(request, call_next)


@app.middleware("http")
async def sql_firewall(request: Request, call_next):
    return await sql_firewall_middleware(request, call_next)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    init_redis(redis_url)
    logger.info("Security proxy started. Redis URL: %s", redis_url)


# ── Generic proxy helper ─────────────────────────────────────────────────────
async def _proxy(request: Request, upstream_base: str, prefix: str) -> Response:
    """Forward an incoming request to an upstream service."""
    path = request.url.path
    query = str(request.url.query)
    upstream_path = path  # keep full path, upstream has matching routes
    url = f"{upstream_base}{upstream_path}"
    if query:
        url += f"?{query}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            upstream_resp = await client.request(
                method=request.method,
                url=url,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=await request.body(),
            )
            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=dict(upstream_resp.headers),
            )
        except httpx.ConnectError:
            return JSONResponse(
                {"error": f"Upstream {prefix} service unavailable"},
                status_code=502,
            )
        except httpx.TimeoutException:
            return JSONResponse(
                {"error": f"Upstream {prefix} service timeout"},
                status_code=504,
            )


# ── LOB routes → lob-engine:8001 ─────────────────────────────────────────────
@app.api_route("/lob/{path:path}", methods=["GET", "POST", "DELETE", "PATCH"])
async def proxy_lob(request: Request, path: str):
    return await _proxy(request, LOB_URL, "lob")


# ── Graph routes → graph-service:8003 ────────────────────────────────────────
@app.api_route("/graph/{path:path}", methods=["GET", "POST"])
async def proxy_graph(request: Request, path: str):
    return await _proxy(request, GRAPH_URL, "graph")


# ── Analytics routes → data-ingestor:8002 ────────────────────────────────────
@app.api_route("/analytics/{path:path}", methods=["GET", "POST"])
async def proxy_analytics(request: Request, path: str):
    return await _proxy(request, ANALYTICS_URL, "analytics")


# ── Quantum routes → quantum-engine:8004 ─────────────────────────────────────
@app.api_route("/quantum/{path:path}", methods=["GET", "POST"])
async def proxy_quantum(request: Request, path: str):
    return await _proxy(request, QUANTUM_URL, "quantum")


# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "module": "security_proxy"}


@app.get("/")
async def root():
    return {"message": "HQT Security Proxy is running"}


# ── Metrics ──────────────────────────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Admin endpoints ───────────────────────────────────────────────────────────
@app.get("/admin/security-events")
async def get_security_events(limit: int = Query(default=100, ge=1, le=1000)):
    """
    Return recent security events from the security_events table.
    Returns an empty list if the table does not yet exist.
    """
    try:
        conn = await asyncpg.connect(PG_DSN)
        try:
            rows = await conn.fetch(
                """
                SELECT event_id, ts, event_type, client_ip, endpoint, raw_payload
                FROM security_events
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
            return {"events": [dict(r) for r in rows], "count": len(rows)}
        finally:
            await conn.close()
    except asyncpg.exceptions.UndefinedTableError:
        logger.info("security_events table does not exist yet — returning empty list")
        return {"events": [], "count": 0}
    except Exception as exc:
        logger.error("Failed to query security_events: %s", exc)
        return JSONResponse({"error": "database error", "detail": str(exc)}, status_code=500)


@app.get("/admin/benchmark-runs")
async def get_benchmark_runs(limit: int = Query(default=50, ge=1, le=500)):
    """
    Return recent benchmark runs from the benchmark_runs table.
    """
    try:
        conn = await asyncpg.connect(PG_DSN)
        try:
            rows = await conn.fetch(
                """
                SELECT run_id, ts, tool, target_endpoint, duration_sec,
                       avg_latency_ms, p99_latency_ms, notes
                FROM benchmark_runs
                ORDER BY ts DESC
                LIMIT $1
                """,
                limit,
            )
            return {"runs": [dict(r) for r in rows], "count": len(rows)}
        finally:
            await conn.close()
    except asyncpg.exceptions.UndefinedTableError:
        logger.info("benchmark_runs table does not exist yet — returning empty list")
        return {"runs": [], "count": 0}
    except Exception as exc:
        logger.error("Failed to query benchmark_runs: %s", exc)
        return JSONResponse({"error": "database error", "detail": str(exc)}, status_code=500)
