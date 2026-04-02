"""
module4_quantum.quantum_service
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Background service that periodically:
  1. Fetches the current FX rate matrix from the graph-service
  2. Runs classical Bellman-Ford arbitrage detection
  3. Runs quantum Grover's algorithm arbitrage detection
  4. Inserts both signals into the arbitrage_signals PostgreSQL table
  5. Records timing into Prometheus histograms

The service loop runs every 10 seconds as an asyncio coroutine so that it
integrates naturally with the FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from typing import Any

import httpx
import psycopg
from prometheus_client import Histogram

from module4_quantum.run_grover import run_grover

logger = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

GRAPH_URL = os.getenv("GRAPH_SERVICE_URL", "http://graph-service:8003")

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

# ─── Prometheus metrics ───────────────────────────────────────────────────────

quantum_grover_ms = Histogram(
    "quantum_grover_ms",
    "Grover run wall-clock time (ms)",
    buckets=[1, 5, 10, 50, 100, 500, 1000, 5000, 10_000, 30_000],
)

quantum_bellman_ford_ms = Histogram(
    "quantum_bellman_ford_ms",
    "Bellman-Ford wall-clock time (ms)",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 25, 50, 100],
)


# ─── Rate matrix fetching ─────────────────────────────────────────────────────

async def _fetch_rates() -> tuple[dict[tuple[str, str], float], list[str]]:
    """Fetch the FX rate matrix from the graph-service.

    Returns
    -------
    (rates, nodes)
        rates : dict mapping (src, dst) → float
        nodes : list of node symbols
    """
    url = f"{GRAPH_URL}/graph/rates"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    nodes: list[str] = data.get("nodes", [])
    matrix: dict[str, Any] = data.get("matrix", {})

    rates: dict[tuple[str, str], float] = {}
    for src, destinations in matrix.items():
        if not isinstance(destinations, dict):
            continue
        for dst, value in destinations.items():
            if src == dst:
                continue
            if value is None:
                continue
            try:
                rates[(src, dst)] = float(value)
            except (TypeError, ValueError):
                continue

    return rates, nodes


# ─── Database helpers ─────────────────────────────────────────────────────────

def _insert_signal(
    conn: psycopg.Connection,
    signal_id: str,
    path: list[str],
    profit_pct: float,
    method: str,
    circuit_depth: int | None,
    classical_ms: float | None,
    quantum_ms: float | None,
    graph_size_n: int,
) -> None:
    """Insert an arbitrage signal into the arbitrage_signals table.

    Parameters
    ----------
    conn:
        Active psycopg connection.
    signal_id:
        UUID string for the signal.
    path:
        Ordered list of symbols representing the arbitrage path (closed cycle).
    profit_pct:
        Profit percentage (e.g. 0.35 means 0.35 %).
    method:
        'CLASSICAL' or 'QUANTUM'.
    circuit_depth:
        For CLASSICAL signals: number of hops (len(path) − 1).
        For QUANTUM signals: Qiskit circuit depth.
    classical_ms:
        Wall-clock time for Bellman-Ford in ms.  NULL for QUANTUM rows.
    quantum_ms:
        Wall-clock time for Grover in ms.  NULL for CLASSICAL rows.
    graph_size_n:
        Number of nodes in the rate graph.
    """
    sql = """
        INSERT INTO arbitrage_signals
            (signal_id, ts, path, profit_pct, method,
             circuit_depth, classical_ms, quantum_ms, graph_size_n)
        VALUES
            (%s, now(), %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (signal_id) DO NOTHING
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                signal_id,
                path,
                round(profit_pct, 6),
                method,
                circuit_depth,
                round(classical_ms, 3) if classical_ms is not None else None,
                round(quantum_ms, 3) if quantum_ms is not None else None,
                graph_size_n,
            ),
        )
    conn.commit()


def _get_db_conn() -> psycopg.Connection:
    """Open and return a synchronous psycopg connection."""
    return psycopg.connect(PG_DSN)


# ─── Main service loop ────────────────────────────────────────────────────────

async def quantum_loop(interval: float = 10.0) -> None:
    """Main service loop - runs forever.

    Every *interval* seconds:
      1. Fetch the FX rate matrix from graph-service
      2. Run Bellman-Ford (classical) and record timing
      3. Run Grover (quantum) and record timing
      4. Insert both signals into PostgreSQL
      5. Update Prometheus histograms

    Errors are caught and logged; the loop continues regardless.
    """
    # Lazy import to avoid circular dependency issues at module load time
    from module3_graph.bellman_ford import (
        bellman_ford_arbitrage,
        compute_cycle_profit,
    )

    logger.info("quantum_loop started (interval=%.1fs)", interval)

    while True:
        try:
            # ── 1. Fetch rates ────────────────────────────────────────────────
            try:
                rates, nodes = await _fetch_rates()
            except Exception as exc:
                logger.warning("quantum_loop: failed to fetch rates: %s", exc)
                await asyncio.sleep(interval)
                continue

            graph_size_n = len(nodes)
            if graph_size_n < 3:
                logger.debug("quantum_loop: only %d nodes - skipping", graph_size_n)
                await asyncio.sleep(interval)
                continue

            # ── 2. Classical Bellman-Ford ─────────────────────────────────────
            classical_ms: float | None = None
            classical_path: list[str] | None = None
            classical_profit_pct: float = 0.0

            try:
                t0 = time.perf_counter()
                classical_path = bellman_ford_arbitrage(rates, nodes)
                classical_ms = (time.perf_counter() - t0) * 1000.0
                quantum_bellman_ford_ms.observe(classical_ms)

                if classical_path and len(classical_path) >= 3:
                    classical_profit_pct = compute_cycle_profit(classical_path, rates)
            except Exception as exc:
                logger.warning("quantum_loop: Bellman-Ford error: %s", exc)

            # ── 3. Quantum Grover ─────────────────────────────────────────────
            quantum_ms: float | None = None
            grover_result: dict | None = None

            try:
                t0 = time.perf_counter()
                # Run in a thread executor to avoid blocking the event loop
                # (AerSimulator is CPU-bound)
                loop = asyncio.get_event_loop()
                grover_result = await loop.run_in_executor(
                    None, _run_grover_sync, rates, nodes
                )
                quantum_ms = (time.perf_counter() - t0) * 1000.0
                quantum_grover_ms.observe(quantum_ms)
            except Exception as exc:
                logger.warning("quantum_loop: Grover error: %s", exc)

            # ── 4. Insert signals ─────────────────────────────────────────────
            try:
                conn = _get_db_conn()
                try:
                    # CLASSICAL signal
                    if classical_path and len(classical_path) >= 3 and classical_profit_pct > 0:
                        _insert_signal(
                            conn=conn,
                            signal_id=str(uuid.uuid4()),
                            path=classical_path,
                            profit_pct=classical_profit_pct,
                            method="CLASSICAL",
                            circuit_depth=len(classical_path) - 1,
                            classical_ms=classical_ms,
                            quantum_ms=None,
                            graph_size_n=graph_size_n,
                        )
                        logger.info(
                            "CLASSICAL signal inserted: %s  profit=%.4f%%  ms=%.2f",
                            " → ".join(classical_path),
                            classical_profit_pct,
                            classical_ms or 0.0,
                        )

                    # QUANTUM signal
                    if grover_result and grover_result.get("path") is not None:
                        grover_path = grover_result["path"]
                        grover_profit = grover_result.get("profit_pct", 0.0)
                        if grover_profit > 0 and len(grover_path) >= 3:
                            _insert_signal(
                                conn=conn,
                                signal_id=str(uuid.uuid4()),
                                path=grover_path,
                                profit_pct=grover_profit,
                                method="QUANTUM",
                                circuit_depth=grover_result.get("circuit_depth"),
                                classical_ms=None,
                                quantum_ms=quantum_ms,
                                graph_size_n=graph_size_n,
                            )
                            logger.info(
                                "QUANTUM signal inserted: %s  profit=%.4f%%  ms=%.2f  depth=%d",
                                " → ".join(grover_path),
                                grover_profit,
                                quantum_ms or 0.0,
                                grover_result.get("circuit_depth", 0),
                            )
                finally:
                    conn.close()
            except Exception as exc:
                logger.warning("quantum_loop: DB insert error: %s", exc)

        except Exception as exc:
            logger.exception("quantum_loop: unexpected error: %s", exc)

        await asyncio.sleep(interval)


def _run_grover_sync(
    rates: dict[tuple[str, str], float],
    nodes: list[str],
) -> dict:
    """Synchronous wrapper around run_grover for use with run_in_executor."""
    return run_grover(rates, nodes, shots=512)
