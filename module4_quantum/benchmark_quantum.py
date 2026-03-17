"""
module4_quantum.benchmark_quantum
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Benchmark Grover's Algorithm vs Bellman-Ford across increasing graph sizes.

Narrative
---------
Bellman-Ford is O(N·E) and completes in < 5ms for all tested graph sizes.
Grover's Algorithm has a theoretical O(√N) query complexity advantage over
classical search, but this advantage applies only to oracle queries on real
quantum hardware — AerSimulator computes the full state vector classically,
resulting in exponential time overhead as N grows. The benchmark demonstrates
the theoretical complexity, not a practical speedup.

Output
------
  bench_out/benchmark_quantum.csv  — per-N timing summary
  bench_out/benchmark_quantum.png  — dual log-scale line chart

CLI
---
  python -m module4_quantum.benchmark_quantum
  python -m module4_quantum.benchmark_quantum --quick   # N ≤ 16, 3 trials
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

import matplotlib
matplotlib.use("Agg")  # headless backend — must be set before pyplot import
import matplotlib.pyplot as plt
import numpy as np
import psycopg

from module3_graph.bellman_ford import benchmark_bellman_ford
from module4_quantum.run_grover import run_grover

logger = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────────────────────

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

OUTPUT_DIR = Path(__file__).parent / "bench_out"

NODE_SIZES_FULL = [4, 8, 12, 16, 20, 24, 28, 32]
NODE_SIZES_QUICK = [4, 8, 12, 16]

TRIALS_FULL = 10
TRIALS_QUICK = 3

GROVER_SHOTS = 256  # fast for benchmarking


# ─── Synthetic rate matrix ─────────────────────────────────────────────────────

def _make_rate_matrix(n_nodes: int) -> tuple[dict[tuple[str, str], float], list[str]]:
    """Generate a fully-connected random rate matrix with rates ∈ [0.8, 1.2]."""
    symbols = [f"N{i}" for i in range(n_nodes)]
    rates: dict[tuple[str, str], float] = {}
    for i, src in enumerate(symbols):
        for j, dst in enumerate(symbols):
            if i != j:
                rates[(src, dst)] = random.uniform(0.8, 1.2)
    return rates, symbols


# ─── Timing helpers ────────────────────────────────────────────────────────────

def _time_grover(n_nodes: int, n_trials: int) -> dict[str, Any]:
    """Run Grover *n_trials* times on a random *n_nodes* graph and return stats."""
    rates, nodes = _make_rate_matrix(n_nodes)
    timings: list[float] = []
    last_result: dict = {}

    for _ in range(n_trials):
        t0 = time.perf_counter()
        last_result = run_grover(rates, nodes, shots=GROVER_SHOTS)
        timings.append((time.perf_counter() - t0) * 1000.0)

    timings_sorted = sorted(timings)
    mean_ms = sum(timings) / len(timings)
    p99_ms = timings_sorted[max(0, int(0.99 * len(timings)) - 1)]

    return {
        "mean_ms": round(mean_ms, 3),
        "p99_ms": round(p99_ms, 3),
        "n_qubits": last_result.get("n_qubits", 0),
        "circuit_depth": last_result.get("circuit_depth", 0),
        "n_iter": last_result.get("n_iter", 0),
    }


def _time_bellman_ford(n_nodes: int, n_trials: int) -> dict[str, Any]:
    """Benchmark Bellman-Ford via the existing benchmark_bellman_ford() helper."""
    result = benchmark_bellman_ford(n_nodes=n_nodes, n_trials=n_trials)
    timings_sorted_approx = result.get("mean_ms", 0.0)  # already averaged

    # benchmark_bellman_ford does not expose p99 directly; re-run manually for it
    rates, nodes = _make_rate_matrix(n_nodes)
    from module3_graph.bellman_ford import bellman_ford_arbitrage
    timings: list[float] = []
    for _ in range(n_trials):
        t0 = time.perf_counter()
        bellman_ford_arbitrage(rates, nodes)
        timings.append((time.perf_counter() - t0) * 1000.0)

    timings_sorted = sorted(timings)
    mean_ms = sum(timings) / len(timings)
    p99_ms = timings_sorted[max(0, int(0.99 * len(timings)) - 1)]

    return {
        "mean_ms": round(mean_ms, 3),
        "p99_ms": round(p99_ms, 3),
    }


# ─── Chart ─────────────────────────────────────────────────────────────────────

def _plot_benchmark(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Produce dual log-scale line chart with O(N) and O(√N) reference lines."""
    node_sizes = [r["n_nodes"] for r in rows]
    bf_means = [r["bf_mean_ms"] for r in rows]
    grover_means = [r["grover_mean_ms"] for r in rows]

    # Reference lines anchored to the first point's Grover mean
    first_grover = grover_means[0] if grover_means[0] > 0 else 1.0
    n0 = node_sizes[0]

    ref_linear = [first_grover * (n / n0) for n in node_sizes]
    ref_sqrt = [first_grover * math.sqrt(n / n0) for n in node_sizes]

    fig, ax = plt.subplots(figsize=(10, 6))

    ax.plot(node_sizes, bf_means, "o-", color="#2196F3", linewidth=2,
            markersize=6, label="Bellman-Ford (mean ms)")
    ax.plot(node_sizes, grover_means, "s-", color="#F44336", linewidth=2,
            markersize=6, label="Grover / AerSimulator (mean ms)")
    ax.plot(node_sizes, ref_linear, "--", color="#9E9E9E", linewidth=1,
            label="O(N) reference")
    ax.plot(node_sizes, ref_sqrt, ":", color="#607D8B", linewidth=1,
            label="O(√N) reference")

    ax.set_yscale("log")
    ax.set_xlabel("Graph Size (N nodes)", fontsize=12)
    ax.set_ylabel("Wall-clock time (ms) — log scale", fontsize=12)
    ax.set_title(
        "Bellman-Ford vs Grover's Algorithm (AerSimulator)\n"
        "HQT Module 4 — Quantum Arbitrage Detection Benchmark",
        fontsize=13,
    )
    ax.legend(fontsize=10)
    ax.grid(True, which="both", linestyle="--", alpha=0.4)
    ax.set_xticks(node_sizes)

    plt.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(output_path), dpi=150)
    plt.close(fig)
    logger.info("Chart saved to %s", output_path)


# ─── DB summary writer ─────────────────────────────────────────────────────────

def _write_db_summary(rows: list[dict[str, Any]]) -> None:
    """Insert a summary row per N into the benchmark_runs table."""
    sql = """
        INSERT INTO benchmark_runs
            (run_id, ts, tool, target_endpoint, duration_sec,
             concurrent_users, total_requests, successful_reqs, failed_reqs,
             avg_latency_ms, p99_latency_ms, notes)
        VALUES (%s, now(), %s, %s, 0, 1, %s, %s, 0, %s, %s, %s)
        ON CONFLICT (run_id) DO NOTHING
    """
    try:
        conn = psycopg.connect(PG_DSN)
        try:
            with conn.cursor() as cur:
                for row in rows:
                    n = row["n_nodes"]
                    notes = (
                        f"Module4 Quantum Benchmark | N={n} | "
                        f"bf_mean={row['bf_mean_ms']}ms | "
                        f"grover_mean={row['grover_mean_ms']}ms | "
                        f"n_qubits={row['n_qubits']} | "
                        f"circuit_depth={row['circuit_depth']} | "
                        f"n_iter={row['n_iter']}"
                    )
                    # BF row
                    cur.execute(
                        sql,
                        (
                            str(uuid.uuid4()),
                            "bellman_ford_benchmark",
                            f"/quantum/benchmark/N{n}",
                            TRIALS_FULL,
                            TRIALS_FULL,
                            row["bf_mean_ms"],
                            row["bf_p99_ms"],
                            notes,
                        ),
                    )
                    # Grover row
                    cur.execute(
                        sql,
                        (
                            str(uuid.uuid4()),
                            "grover_benchmark",
                            f"/quantum/benchmark/N{n}",
                            TRIALS_FULL,
                            TRIALS_FULL,
                            row["grover_mean_ms"],
                            row["grover_p99_ms"],
                            notes,
                        ),
                    )
            conn.commit()
            logger.info("Benchmark summary written to benchmark_runs table")
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not write benchmark summary to DB: %s", exc)


# ─── Main benchmark runner ────────────────────────────────────────────────────

def run_benchmark(quick: bool = False) -> list[dict[str, Any]]:
    """Run the full benchmark and return the summary rows.

    Parameters
    ----------
    quick:
        If True, use NODE_SIZES_QUICK and TRIALS_QUICK (faster, for CI).

    Returns
    -------
    list[dict]
        One dict per N with keys:
        n_nodes, bf_mean_ms, bf_p99_ms, grover_mean_ms, grover_p99_ms,
        n_qubits, circuit_depth, n_iter
    """
    node_sizes = NODE_SIZES_QUICK if quick else NODE_SIZES_FULL
    n_trials = TRIALS_QUICK if quick else TRIALS_FULL

    rows: list[dict[str, Any]] = []

    for n in node_sizes:
        logger.info("Benchmarking N=%d (%d trials each)…", n, n_trials)

        bf_stats = _time_bellman_ford(n, n_trials)
        grover_stats = _time_grover(n, n_trials)

        row = {
            "n_nodes": n,
            "bf_mean_ms": bf_stats["mean_ms"],
            "bf_p99_ms": bf_stats["p99_ms"],
            "grover_mean_ms": grover_stats["mean_ms"],
            "grover_p99_ms": grover_stats["p99_ms"],
            "n_qubits": grover_stats["n_qubits"],
            "circuit_depth": grover_stats["circuit_depth"],
            "n_iter": grover_stats["n_iter"],
        }
        rows.append(row)
        logger.info(
            "  N=%d  BF=%.2fms  Grover=%.2fms  qubits=%d  depth=%d  iter=%d",
            n,
            bf_stats["mean_ms"],
            grover_stats["mean_ms"],
            grover_stats["n_qubits"],
            grover_stats["circuit_depth"],
            grover_stats["n_iter"],
        )

    return rows


def save_csv(rows: list[dict[str, Any]], output_path: Path) -> None:
    """Write benchmark rows to a CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "n_nodes", "bf_mean_ms", "bf_p99_ms",
        "grover_mean_ms", "grover_p99_ms",
        "n_qubits", "circuit_depth", "n_iter",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("CSV saved to %s", output_path)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Benchmark Grover's Algorithm vs Bellman-Ford (HQT Module 4)"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick mode: N up to 16 only, 3 trials per method",
    )
    args = parser.parse_args()

    rows = run_benchmark(quick=args.quick)

    csv_path = OUTPUT_DIR / "benchmark_quantum.csv"
    png_path = OUTPUT_DIR / "benchmark_quantum.png"

    save_csv(rows, csv_path)
    _plot_benchmark(rows, png_path)
    _write_db_summary(rows)

    # Print summary table to stdout
    header = f"{'N':>4}  {'BF mean':>10}  {'BF p99':>10}  {'Grover mean':>12}  {'Grover p99':>10}  {'Qubits':>6}  {'Depth':>6}  {'Iter':>4}"
    print("\n" + header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['n_nodes']:>4}  "
            f"{r['bf_mean_ms']:>9.3f}ms  "
            f"{r['bf_p99_ms']:>9.3f}ms  "
            f"{r['grover_mean_ms']:>11.3f}ms  "
            f"{r['grover_p99_ms']:>9.3f}ms  "
            f"{r['n_qubits']:>6}  "
            f"{r['circuit_depth']:>6}  "
            f"{r['n_iter']:>4}"
        )
    print()


if __name__ == "__main__":
    main()
