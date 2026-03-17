"""
HQT Quantum Engine API
~~~~~~~~~~~~~~~~~~~~~~~
FastAPI application for Module 4 — Quantum Arbitrage Detection.

Endpoints
---------
  GET  /health                    — liveness probe (shared with /quantum/health)
  GET  /quantum/health            — alias for /health
  POST /quantum/run-grover        — run Grover or Bellman-Ford on synthetic data
  GET  /quantum/signals           — query arbitrage_signals table
  GET  /quantum/benchmark         — return latest benchmark_quantum.csv as JSON
  GET  /metrics                   — Prometheus metrics

Background
----------
  On startup the quantum_loop coroutine is launched as an asyncio background
  task.  It runs every 10 seconds, fetching the live rate matrix from the
  graph-service and inserting both CLASSICAL and QUANTUM signals to PostgreSQL.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Query
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from starlette.responses import Response

from module4_quantum.quantum_service import quantum_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("quantum_api")

# ─── Database DSN ─────────────────────────────────────────────────────────────

def _dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'postgres')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'hqt')} "
        f"user={os.getenv('POSTGRES_USER', 'hqt')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    )


# ─── Lifespan ─────────────────────────────────────────────────────────────────

_quantum_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _quantum_task
    _quantum_task = asyncio.create_task(quantum_loop(interval=10.0))
    logger.info("Quantum loop background task started")

    yield

    if _quantum_task:
        _quantum_task.cancel()
        try:
            await _quantum_task
        except asyncio.CancelledError:
            pass
    logger.info("Quantum loop stopped")


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="HQT Quantum Engine API",
    description=(
        "Module 4 — Grover's algorithm arbitrage detection engine.  "
        "Runs classical Bellman-Ford and quantum Grover search in parallel "
        "on the live FX rate graph."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# ─── Pydantic models ─────────────────────────────────────────────────────────

class RunGroverRequest(BaseModel):
    graph_size_n: int = Field(
        default=8,
        ge=3,
        le=32,
        description="Number of synthetic nodes (assets) in the rate matrix",
    )
    method: str = Field(
        default="BOTH",
        description="Which algorithm to run: GROVER, CLASSICAL, or BOTH",
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_synthetic_rates(
    n: int,
) -> tuple[dict[tuple[str, str], float], list[str]]:
    """Build a fully-connected synthetic rate matrix with rates ∈ [0.8, 1.2]."""
    symbols = [f"N{i}" for i in range(n)]
    rates: dict[tuple[str, str], float] = {}
    for i, src in enumerate(symbols):
        for j, dst in enumerate(symbols):
            if i != j:
                rates[(src, dst)] = random.uniform(0.8, 1.2)
    return rates, symbols


BENCH_CSV = Path(__file__).parent / "bench_out" / "benchmark_quantum.csv"


# ─── Health ──────────────────────────────────────────────────────────────────

@app.get("/health")
@app.get("/quantum/health")
async def health() -> dict:
    """Liveness / readiness probe."""
    task_status = (
        "running"
        if _quantum_task and not _quantum_task.done()
        else "stopped"
    )
    return {
        "status": "ok",
        "module": "quantum_engine",
        "quantum_loop": task_status,
    }


# ─── POST /quantum/run-grover ─────────────────────────────────────────────────

@app.post("/quantum/run-grover")
async def post_run_grover(body: RunGroverRequest) -> dict[str, Any]:
    """Run Grover and/or Bellman-Ford on a synthetic random rate matrix.

    Parameters
    ----------
    graph_size_n:
        Number of synthetic nodes.  Must be between 3 and 32.
    method:
        ``"GROVER"``     — run Grover's algorithm only
        ``"CLASSICAL"``  — run Bellman-Ford only
        ``"BOTH"``       — run both and return combined results (default)
    """
    method = body.method.upper()
    if method not in {"GROVER", "CLASSICAL", "BOTH"}:
        raise HTTPException(
            status_code=422,
            detail=f"method must be GROVER, CLASSICAL, or BOTH — got '{body.method}'",
        )

    rates, nodes = _make_synthetic_rates(body.graph_size_n)
    result: dict[str, Any] = {
        "graph_size_n": body.graph_size_n,
        "method": method,
        "nodes": nodes,
    }

    if method in {"GROVER", "BOTH"}:
        from module4_quantum.run_grover import run_grover

        loop = asyncio.get_event_loop()
        grover_result = await loop.run_in_executor(
            None,
            lambda: run_grover(rates, nodes, shots=512),
        )
        result["grover"] = grover_result

    if method in {"CLASSICAL", "BOTH"}:
        from module3_graph.bellman_ford import (
            bellman_ford_arbitrage,
            compute_cycle_profit,
        )
        import time

        t0 = time.perf_counter()
        path = bellman_ford_arbitrage(rates, nodes)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        profit_pct = 0.0
        if path and len(path) >= 3:
            profit_pct = compute_cycle_profit(path, rates)

        result["classical"] = {
            "path": path,
            "profit_pct": round(profit_pct, 6),
            "classical_ms": round(elapsed_ms, 3),
            "circuit_depth": (len(path) - 1) if path else 0,
        }

    return result


# ─── GET /quantum/signals ─────────────────────────────────────────────────────

@app.get("/quantum/signals")
async def get_signals(
    limit: int = Query(default=50, ge=1, le=500, description="Max rows to return"),
    method: str = Query(default="ALL", description="Filter: QUANTUM, CLASSICAL, or ALL"),
) -> dict[str, Any]:
    """Return recent arbitrage signals from the database.

    Parameters
    ----------
    limit:
        Maximum number of rows to return (1–500, default 50).
    method:
        ``"QUANTUM"``    — only quantum Grover signals
        ``"CLASSICAL"``  — only Bellman-Ford signals
        ``"ALL"``        — both (default)
    """
    method_upper = method.upper()
    if method_upper not in {"QUANTUM", "CLASSICAL", "ALL"}:
        raise HTTPException(
            status_code=422,
            detail=f"method must be QUANTUM, CLASSICAL, or ALL — got '{method}'",
        )

    try:
        conn = psycopg.connect(_dsn(), autocommit=True)
        try:
            if method_upper == "ALL":
                sql = """
                    SELECT signal_id, ts, path, profit_pct, method,
                           circuit_depth, classical_ms, quantum_ms, graph_size_n
                    FROM arbitrage_signals
                    ORDER BY ts DESC
                    LIMIT %s
                """
                params = (limit,)
            else:
                sql = """
                    SELECT signal_id, ts, path, profit_pct, method,
                           circuit_depth, classical_ms, quantum_ms, graph_size_n
                    FROM arbitrage_signals
                    WHERE method = %s
                    ORDER BY ts DESC
                    LIMIT %s
                """
                params = (method_upper, limit)

            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                col_names = [
                    "signal_id", "ts", "path", "profit_pct", "method",
                    "circuit_depth", "classical_ms", "quantum_ms", "graph_size_n",
                ]
                signals = [
                    {col: (str(val) if not isinstance(val, (int, float, list, type(None))) else val)
                     for col, val in zip(col_names, row)}
                    for row in rows
                ]
        finally:
            conn.close()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error: {exc}")

    return {
        "count": len(signals),
        "method_filter": method_upper,
        "signals": signals,
    }


# ─── GET /quantum/benchmark ───────────────────────────────────────────────────

@app.get("/quantum/benchmark")
async def get_benchmark() -> dict[str, Any]:
    """Return the latest benchmark results from benchmark_quantum.csv.

    Returns an empty dict if the benchmark has not been run yet.
    """
    if not BENCH_CSV.exists():
        return {
            "available": False,
            "message": (
                "No benchmark data found. "
                "Run: python -m module4_quantum.benchmark_quantum"
            ),
            "rows": [],
        }

    rows: list[dict[str, Any]] = []
    try:
        with open(BENCH_CSV, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                typed_row: dict[str, Any] = {}
                for k, v in row.items():
                    try:
                        typed_row[k] = float(v) if "." in v else int(v)
                    except (ValueError, TypeError):
                        typed_row[k] = v
                rows.append(typed_row)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read benchmark CSV: {exc}",
        )

    return {
        "available": True,
        "csv_path": str(BENCH_CSV),
        "row_count": len(rows),
        "rows": rows,
    }


# ─── GET /metrics ─────────────────────────────────────────────────────────────

@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
