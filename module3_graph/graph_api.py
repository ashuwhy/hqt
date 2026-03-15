"""
module3_graph.graph_api
~~~~~~~~~~~~~~~~~~~~~~~
FastAPI REST API for the FX exchange-rate graph and Bellman-Ford
arbitrage engine.

Endpoints:
    GET /health, /graph/health
    GET /graph/nodes
    GET /graph/edges
    GET /graph/paths?from_symbol=USD
    GET /graph/rates
    GET /graph/benchmark

On startup:
    1. Initialise AGE graph (idempotent)
    2. Launch edge-weight updater  (real LOB → graph)
    3. Launch Bellman-Ford detector (→ arbitrage_signals table)
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Query
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from starlette.responses import Response

from module3_graph.graph_init import init_graph
from module3_graph.bellman_ford import (
    benchmark_bellman_ford,
    build_rate_matrix,
    run_detector,
)
from module3_graph.edge_weight_updater import run_updater
from module3_graph.graph_queries import (
    crypto_subgraph,
    find_3hop_arbitrage_cycles,
    find_high_spread_edges,
    find_shortest_path,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("graph_api")

# ── Database ─────────────────────────────────────────────────────────────────
def _dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'hqt')} "
        f"user={os.getenv('POSTGRES_USER', 'hqt')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    )


def _cypher_count(conn: psycopg.Connection, query: str) -> int:
    sql = f"SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ {query} $cypher$) AS (v agtype);"
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        cur.execute(sql)
        row = cur.fetchone()
        return int(str(row[0])) if row else 0


# ── Lifespan ─────────────────────────────────────────────────────────────────
updater_task: asyncio.Task | None = None
detector_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global updater_task, detector_task

    # Dedicated connections for long-running background tasks
    graph_conn = psycopg.connect(_dsn(), autocommit=False)
    updater_conn = psycopg.connect(_dsn(), autocommit=True)
    detector_conn = psycopg.connect(_dsn(), autocommit=True)

    # 1. Initialise graph (idempotent)
    try:
        result = init_graph(graph_conn)
        logger.info("Graph init: %s", result)
    except Exception as exc:
        logger.error("Graph init failed: %s", exc)

    graph_conn.close()

    # 2. Launch edge-weight updater (real data from LOB + TimescaleDB)
    updater_task = asyncio.create_task(run_updater(updater_conn))
    logger.info("Edge weight updater background task started")

    # 3. Launch Bellman-Ford detector
    detector_task = asyncio.create_task(run_detector(detector_conn, interval=0.5))
    logger.info("Bellman-Ford detector background task started")

    yield

    # Shutdown
    for task in (updater_task, detector_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    for conn in (updater_conn, detector_conn):
        try:
            conn.close()
        except Exception:
            pass


app = FastAPI(title="HQT Graph API", lifespan=lifespan)


# ── Health ───────────────────────────────────────────────────────────────────
@app.get("/health")
@app.get("/graph/health")
async def health():
    try:
        conn = psycopg.connect(_dsn(), autocommit=True)
        node_count = _cypher_count(conn, "MATCH (a:Asset) RETURN count(a)")
        edge_count = _cypher_count(conn, "MATCH ()-[r:EXCHANGE]->() RETURN count(r)")

        # Get last update timestamp from any edge
        sql = """
            SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$
                MATCH ()-[r:EXCHANGE]->()
                RETURN r.last_updated
                ORDER BY r.last_updated DESC
                LIMIT 1
            $cypher$) AS (v agtype);
        """
        with conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(sql)
            row = cur.fetchone()
            last_updated = int(str(row[0])) if row else None

        conn.close()

        return {
            "status": "ok",
            "module": "graph_service",
            "node_count": node_count,
            "edge_count": edge_count,
            "last_edge_update": last_updated,
            "updater_status": "running" if updater_task and not updater_task.done() else "stopped",
            "detector_status": "running" if detector_task and not detector_task.done() else "stopped",
        }
    except Exception as exc:
        return {"status": "degraded", "module": "graph_service", "error": str(exc)}


# ── GET /graph/nodes ─────────────────────────────────────────────────────────
@app.get("/graph/nodes")
async def get_nodes():
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        sql = """
            SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$
                MATCH (a:Asset)
                RETURN a.symbol, a.asset_type
            $cypher$) AS (symbol agtype, asset_type agtype);
        """
        with conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(sql)
            rows = cur.fetchall()

        return [
            {"symbol": str(r[0]).strip('"'), "asset_type": str(r[1]).strip('"')}
            for r in rows
        ]
    finally:
        conn.close()


# ── GET /graph/edges ─────────────────────────────────────────────────────────
@app.get("/graph/edges")
async def get_edges():
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        sql = """
            SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$
                MATCH (a:Asset)-[r:EXCHANGE]->(b:Asset)
                RETURN a.symbol, b.symbol, r.bid, r.ask, r.spread, r.last_updated
            $cypher$) AS (src agtype, dst agtype, bid agtype, ask agtype, spread agtype, ts agtype);
        """
        with conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(sql)
            rows = cur.fetchall()

        edges = []
        for r in rows:
            try:
                edges.append({
                    "src": str(r[0]).strip('"'),
                    "dst": str(r[1]).strip('"'),
                    "bid": float(str(r[2])),
                    "ask": float(str(r[3])),
                    "spread": float(str(r[4])),
                    "last_updated": int(str(r[5])) if r[5] else None,
                })
            except (ValueError, TypeError):
                continue
        return edges
    finally:
        conn.close()


# ── GET /graph/paths ─────────────────────────────────────────────────────────
@app.get("/graph/paths")
async def get_paths(from_symbol: str = Query("USD", description="Starting symbol")):
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        cycles = find_3hop_arbitrage_cycles(conn, from_symbol)
        return {"from_symbol": from_symbol, "cycles": cycles, "count": len(cycles)}
    finally:
        conn.close()


# ── GET /graph/rates ─────────────────────────────────────────────────────────
@app.get("/graph/rates")
async def get_rates():
    """Return N×N adjacency matrix JSON — consumed by Module 4 quantum_service.py."""
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        rates, nodes = build_rate_matrix(conn)

        # Build NxN matrix
        matrix: dict[str, dict[str, float | None]] = {}
        for src in nodes:
            matrix[src] = {}
            for dst in nodes:
                if src == dst:
                    matrix[src][dst] = 1.0
                else:
                    matrix[src][dst] = rates.get((src, dst))

        return {
            "nodes": nodes,
            "matrix": matrix,
            "size": len(nodes),
        }
    finally:
        conn.close()


# ── GET /graph/shortest ──────────────────────────────────────────────────────
@app.get("/graph/shortest")
async def get_shortest(
    from_sym: str = Query(..., description="Source symbol"),
    to_sym: str = Query(..., description="Target symbol"),
):
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        result = find_shortest_path(conn, from_sym, to_sym)
        if result is None:
            return {"error": f"No path found from {from_sym} to {to_sym}"}
        return result
    finally:
        conn.close()


# ── GET /graph/high-spread ───────────────────────────────────────────────────
@app.get("/graph/high-spread")
async def get_high_spread(threshold: float = Query(0.01, description="Minimum spread")):
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        edges = find_high_spread_edges(conn, threshold)
        return {"threshold": threshold, "edges": edges, "count": len(edges)}
    finally:
        conn.close()


# ── GET /graph/crypto ────────────────────────────────────────────────────────
@app.get("/graph/crypto")
async def get_crypto_subgraph():
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        return crypto_subgraph(conn)
    finally:
        conn.close()


# ── GET /graph/benchmark ────────────────────────────────────────────────────
@app.get("/graph/benchmark")
async def get_benchmark(
    n_nodes: int = Query(20, ge=3, le=100),
    n_trials: int = Query(100, ge=1, le=1000),
):
    """Benchmark Bellman-Ford for Module 4 comparison chart."""
    return benchmark_bellman_ford(n_nodes, n_trials)


# ── GET /graph/signals ──────────────────────────────────────────────────────
@app.get("/graph/signals")
async def get_signals(limit: int = Query(50, ge=1, le=500)):
    """Return recent CLASSICAL arbitrage signals from the database."""
    conn = psycopg.connect(_dsn(), autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT signal_id, ts, path, profit_pct, method, classical_ms, graph_size_n
                FROM arbitrage_signals
                WHERE method = 'CLASSICAL'
                ORDER BY ts DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()

        return [
            {
                "signal_id": str(r[0]),
                "ts": r[1].isoformat() if r[1] else None,
                "path": r[2],
                "profit_pct": float(r[3]),
                "method": r[4],
                "classical_ms": float(r[5]) if r[5] else None,
                "graph_size_n": r[6],
            }
            for r in rows
        ]
    finally:
        conn.close()


# ── Metrics ──────────────────────────────────────────────────────────────────
@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
