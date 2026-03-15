"""
HQT Security Proxy — Central API gateway.

Routes traffic to backend microservices:
    /lob/*     → lob-engine:8001
    /graph/*   → graph-service:8003
    /quantum/* → quantum-engine:8004

Also hosts /health and /metrics endpoints directly.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("security_proxy")

app = FastAPI(title="HQT Security Proxy")

# ── Upstream service URLs ────────────────────────────────────────────────────
LOB_URL = os.getenv("LOB_ENGINE_URL", "http://lob-engine:8001")
GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")
QUANTUM_URL = os.getenv("QUANTUM_ENGINE_URL", "http://quantum-engine:8004")
ANALYTICS_URL = os.getenv("ANALYTICS_URL", "http://data-ingestor:8002")


async def _proxy(request: Request, upstream_base: str, prefix: str) -> Response:
    """Forward an incoming request to an upstream service."""
    # Build the upstream URL: strip leading prefix, keep the rest
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


# ── Graph routes → graph-service:8003 ────────────────────────────────────────
@app.api_route("/graph/{path:path}", methods=["GET", "POST"])
async def proxy_graph(request: Request, path: str):
    return await _proxy(request, GRAPH_URL, "graph")


# ── Analytics routes → data-ingestor:8002 ────────────────────────────────────
@app.api_route("/analytics/{path:path}", methods=["GET", "POST"])
async def proxy_analytics(request: Request, path: str):
    return await _proxy(request, ANALYTICS_URL, "analytics")


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
