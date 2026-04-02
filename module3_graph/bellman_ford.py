"""
module3_graph.bellman_ford
~~~~~~~~~~~~~~~~~~~~~~~~~~
PRIMARY ARBITRAGE ENGINE - Bellman-Ford negative-cycle detection.

Architecture decision (ADR March 9 2026):
    Bellman-Ford is the production algorithm.  It runs continuously,
    finds all profitable cycles deterministically in < 5 ms, and
    saves ``method='CLASSICAL'`` signals.  Quantum (Phase 4) consumes
    ``/graph/rates`` for research benchmarking only.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


# ─── Rate matrix construction ────────────────────────────────────────────────

def build_rate_matrix(conn: psycopg.Connection) -> tuple[dict[tuple[str, str], float], list[str]]:
    """Query all EXCHANGE edges from the AGE graph and build a rate map.

    Returns:
        (rates, nodes)
        rates : dict mapping ``(src, dst)`` → ``bid`` rate (float)
        nodes : sorted list of unique node symbols
    """
    sql = """
        SELECT * FROM ag_catalog.cypher('fx_graph', $$
            MATCH (a:Asset)-[r:EXCHANGE]->(b:Asset)
            RETURN a.symbol, b.symbol, r.bid
        $$) AS (src agtype, dst agtype, bid agtype);
    """
    rates: dict[tuple[str, str], float] = {}
    node_set: set[str] = set()

    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        cur.execute(sql)
        for row in cur.fetchall():
            src = str(row[0]).strip('"')
            dst = str(row[1]).strip('"')
            bid = float(str(row[2]))
            if bid > 0:
                rates[(src, dst)] = bid
                node_set.update((src, dst))

    nodes = sorted(node_set)
    return rates, nodes


# ─── Bellman-Ford core ───────────────────────────────────────────────────────

def bellman_ford_arbitrage(
    rates: dict[tuple[str, str], float],
    nodes: list[str],
) -> list[str] | None:
    """Detect arbitrage using Bellman-Ford on -log(rate) weights.

    Returns the cycle path as a list of symbols if a profitable cycle
    exists, otherwise ``None``.

    Complexity: O(V * E) - runs in < 5 ms for N ≤ 20 nodes.
    """
    if not rates or not nodes:
        return None

    n = len(nodes)
    idx = {sym: i for i, sym in enumerate(nodes)}

    # Build edge list: (u, v, weight) where weight = -log(rate)
    edges: list[tuple[int, int, float]] = []
    for (src, dst), rate in rates.items():
        if rate > 0 and src in idx and dst in idx:
            edges.append((idx[src], idx[dst], -math.log(rate)))

    # Run from every source to maximise detection
    for source in range(n):
        dist = [float("inf")] * n
        pred = [-1] * n
        dist[source] = 0.0

        # N-1 relaxation passes
        for _ in range(n - 1):
            updated = False
            for u, v, w in edges:
                if dist[u] + w < dist[v] - 1e-12:
                    dist[v] = dist[u] + w
                    pred[v] = u
                    updated = True
            if not updated:
                break

        # Nth pass - detect negative cycle
        for u, v, w in edges:
            if dist[u] + w < dist[v] - 1e-12:
                cycle = extract_cycle(pred, v, n, nodes)
                if cycle and len(cycle) >= 3:
                    return cycle

    return None


def extract_cycle(
    predecessor: list[int],
    start: int,
    n: int,
    symbols: list[str],
) -> list[str]:
    """Walk the predecessor map to extract the full negative cycle.

    We walk back from ``start`` through predecessors for at most ``n``
    steps to ensure we're inside the cycle, then collect the cycle.
    """
    # Walk n steps back to ensure we land inside the cycle
    node = start
    for _ in range(n):
        node = predecessor[node]
        if node == -1:
            return []

    # Collect the cycle
    cycle_start = node
    cycle = [symbols[cycle_start]]
    current = predecessor[cycle_start]
    seen_count = 0
    while current != cycle_start and seen_count < n:
        cycle.append(symbols[current])
        current = predecessor[current]
        seen_count += 1
        if current == -1:
            return []

    cycle.append(symbols[cycle_start])  # close the cycle
    cycle.reverse()
    return cycle


def compute_cycle_profit(cycle: list[str], rates: dict[tuple[str, str], float]) -> float:
    """Compute the profit percentage of traversing a cycle.

    Returns profit as a percentage (e.g., 0.5 means 0.5% profit).
    """
    product = 1.0
    for i in range(len(cycle) - 1):
        edge = (cycle[i], cycle[i + 1])
        rate = rates.get(edge, 0.0)
        if rate <= 0:
            return -100.0
        product *= rate
    return (product - 1.0) * 100.0


# ─── Continuous detector coroutine ───────────────────────────────────────────

async def run_detector(conn: psycopg.Connection, interval: float = 0.5) -> None:
    """Run Bellman-Ford every ``interval`` seconds and insert CLASSICAL signals.

    This coroutine runs forever as a background task.
    """
    import asyncio

    logger.info("Bellman-Ford detector started (interval=%.1fs)", interval)

    while True:
        try:
            t0 = time.perf_counter()
            rates, nodes = build_rate_matrix(conn)
            cycle = bellman_ford_arbitrage(rates, nodes)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            if cycle and len(cycle) >= 3:
                profit_pct = compute_cycle_profit(cycle, rates)
                if profit_pct > 0:
                    _insert_signal(conn, cycle, profit_pct, elapsed_ms, len(nodes))
                    logger.info(
                        "Arbitrage detected: %s  profit=%.4f%%  time=%.2fms",
                        " → ".join(cycle), profit_pct, elapsed_ms,
                    )
        except Exception:
            logger.exception("Bellman-Ford detector error (continuing)")

        await asyncio.sleep(interval)


def _insert_signal(
    conn: psycopg.Connection,
    path: list[str],
    profit_pct: float,
    classical_ms: float,
    graph_size_n: int,
) -> None:
    """Insert a CLASSICAL arbitrage signal into the arbitrage_signals table."""
    sql = """
        INSERT INTO arbitrage_signals (signal_id, ts, path, profit_pct, method, circuit_depth, classical_ms, graph_size_n)
        VALUES (%s, now(), %s, %s, 'CLASSICAL', %s, %s, %s)
    """
    circuit_depth = len(path) - 1  # hops = nodes - 1 (cycle is closed)
    with conn.cursor() as cur:
        cur.execute(sql, (str(uuid.uuid4()), path, round(profit_pct, 6), circuit_depth, round(classical_ms, 3), graph_size_n))
    conn.commit()


# ─── Benchmark utility ──────────────────────────────────────────────────────

def benchmark_bellman_ford(n_nodes: int = 20, n_trials: int = 100) -> dict[str, Any]:
    """Generate a random rate matrix and benchmark Bellman-Ford.

    Returns timing stats dict for Module 4 comparison chart.
    """
    import random

    symbols = [f"N{i}" for i in range(n_nodes)]
    rates: dict[tuple[str, str], float] = {}
    for i, s in enumerate(symbols):
        for j, d in enumerate(symbols):
            if i != j:
                rates[(s, d)] = random.uniform(0.5, 1.5)

    timings = []
    for _ in range(n_trials):
        t0 = time.perf_counter()
        bellman_ford_arbitrage(rates, symbols)
        timings.append((time.perf_counter() - t0) * 1000)

    return {
        "n_nodes": n_nodes,
        "n_edges": len(rates),
        "n_trials": n_trials,
        "mean_ms": round(sum(timings) / len(timings), 3),
        "min_ms": round(min(timings), 3),
        "max_ms": round(max(timings), 3),
        "median_ms": round(sorted(timings)[len(timings) // 2], 3),
    }
