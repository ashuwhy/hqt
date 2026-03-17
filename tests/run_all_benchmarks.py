#!/usr/bin/env python3
"""
Unified Benchmark Runner for HQT — Modules 1–5.

Runs all production benchmarks and generates a consolidated summary report.

Usage:
    python tests/run_all_benchmarks.py           # full benchmark
    python tests/run_all_benchmarks.py --quick    # fast mode (CI)

Benchmarks:
    1. Bellman-Ford performance at N=4,8,12,16,20,24,28,32
    2. Grover vs Bellman-Ford comparison (quantum module)
    3. Overall summary with production readiness assessment
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from module3_graph.bellman_ford import bellman_ford_arbitrage, compute_cycle_profit, benchmark_bellman_ford
from module4_quantum.run_grover import run_grover, enumerate_cycles, is_profitable


# ── Configuration ─────────────────────────────────────────────────────────────

OUTPUT_DIR = Path(__file__).parent.parent / "bench_results"
QUICK_SIZES = [4, 8, 12, 16]
FULL_SIZES = [4, 8, 12, 16, 20, 24, 28, 32]
QUICK_TRIALS = 3
FULL_TRIALS = 10


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_rate_matrix(n: int, seed: int = 42) -> tuple[dict, list[str]]:
    """Fully-connected random rate matrix with rates ∈ [0.8, 1.2]."""
    rng = random.Random(seed)
    nodes = [f"N{i}" for i in range(n)]
    rates = {(s, d): rng.uniform(0.8, 1.2) for s in nodes for d in nodes if s != d}
    return rates, nodes


def _benchmark_bellman_ford(n: int, trials: int) -> dict[str, Any]:
    """Benchmark BF at node size N."""
    rates, nodes = _make_rate_matrix(n)
    timings = []
    cycle_found = False
    for _ in range(trials):
        t0 = time.perf_counter()
        cycle = bellman_ford_arbitrage(rates, nodes)
        timings.append((time.perf_counter() - t0) * 1000.0)
        if cycle is not None:
            cycle_found = True

    timings_sorted = sorted(timings)
    return {
        "n_nodes": n,
        "n_edges": len(rates),
        "mean_ms": round(sum(timings) / len(timings), 3),
        "median_ms": round(timings_sorted[len(timings) // 2], 3),
        "min_ms": round(min(timings), 3),
        "max_ms": round(max(timings), 3),
        "p99_ms": round(timings_sorted[max(0, int(0.99 * len(timings)) - 1)], 3),
        "cycle_found": cycle_found,
    }


def _benchmark_grover(n: int, trials: int) -> dict[str, Any]:
    """Benchmark Grover at node size N."""
    rates, nodes = _make_rate_matrix(n)
    timings = []
    last_result = {}
    for _ in range(trials):
        t0 = time.perf_counter()
        last_result = run_grover(rates, nodes, shots=256)
        timings.append((time.perf_counter() - t0) * 1000.0)

    timings_sorted = sorted(timings)
    return {
        "n_nodes": n,
        "mean_ms": round(sum(timings) / len(timings), 3),
        "median_ms": round(timings_sorted[len(timings) // 2], 3),
        "min_ms": round(min(timings), 3),
        "max_ms": round(max(timings), 3),
        "p99_ms": round(timings_sorted[max(0, int(0.99 * len(timings)) - 1)], 3),
        "n_qubits": last_result.get("n_qubits", 0),
        "circuit_depth": last_result.get("circuit_depth", 0),
        "n_iter": last_result.get("n_iter", 0),
        "n_profitable": last_result.get("n_profitable", 0),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="HQT Unified Benchmark Runner")
    parser.add_argument("--quick", action="store_true", help="Quick mode (fewer sizes, fewer trials)")
    args = parser.parse_args()

    sizes = QUICK_SIZES if args.quick else FULL_SIZES
    trials = QUICK_TRIALS if args.quick else FULL_TRIALS
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("  HQT Production Benchmark Suite")
    print(f"  Mode: {'QUICK' if args.quick else 'FULL'}  |  Sizes: {sizes}  |  Trials: {trials}")
    print(f"  Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    # ── 1. Bellman-Ford Benchmark ─────────────────────────────────────────────
    print("\n── Bellman-Ford Performance ──────────────────────────────────────")
    bf_results = []
    for n in sizes:
        result = _benchmark_bellman_ford(n, trials)
        bf_results.append(result)
        print(
            f"  N={n:>3}  edges={result['n_edges']:>4}  "
            f"mean={result['mean_ms']:>8.3f}ms  "
            f"median={result['median_ms']:>8.3f}ms  "
            f"p99={result['p99_ms']:>8.3f}ms  "
            f"{'✓ cycle' if result['cycle_found'] else '– none'}"
        )

    # ── 2. Grover Benchmark (if sizes are small enough) ───────────────────────
    grover_sizes = [n for n in sizes if n <= 16]  # Grover is very slow above 16
    print("\n── Grover's Algorithm (AerSimulator) ────────────────────────────")
    grover_results = []
    for n in grover_sizes:
        result = _benchmark_grover(n, trials)
        grover_results.append(result)
        print(
            f"  N={n:>3}  "
            f"mean={result['mean_ms']:>10.3f}ms  "
            f"qubits={result['n_qubits']:>3}  "
            f"depth={result['circuit_depth']:>5}  "
            f"iter={result['n_iter']:>3}  "
            f"profitable={result['n_profitable']}"
        )

    # ── 3. Save CSV ───────────────────────────────────────────────────────────
    csv_path = OUTPUT_DIR / "benchmark_summary.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "algorithm", "n_nodes", "n_edges", "mean_ms", "median_ms",
            "min_ms", "max_ms", "p99_ms", "n_qubits", "circuit_depth",
        ])
        for r in bf_results:
            writer.writerow([
                "bellman_ford", r["n_nodes"], r["n_edges"],
                r["mean_ms"], r["median_ms"], r["min_ms"], r["max_ms"], r["p99_ms"],
                "", "",
            ])
        for r in grover_results:
            writer.writerow([
                "grover", r["n_nodes"], "",
                r["mean_ms"], r["median_ms"], r["min_ms"], r["max_ms"], r["p99_ms"],
                r["n_qubits"], r["circuit_depth"],
            ])

    # ── 4. Production Readiness Assessment ────────────────────────────────────
    print("\n── Production Readiness Assessment ──────────────────────────────")
    all_pass = True

    # Check BF < 5ms at N=20
    bf_20 = next((r for r in bf_results if r["n_nodes"] == 20), None)
    if bf_20:
        if bf_20["median_ms"] < 5.0:
            print(f"  ✅ BF N=20 median={bf_20['median_ms']:.3f}ms < 5ms target")
        else:
            print(f"  ❌ BF N=20 median={bf_20['median_ms']:.3f}ms EXCEEDS 5ms target")
            all_pass = False
    else:
        print("  ⏭  BF N=20 not tested (quick mode)")

    # Check BF < 15ms at N=32
    bf_32 = next((r for r in bf_results if r["n_nodes"] == 32), None)
    if bf_32:
        if bf_32["median_ms"] < 15.0:
            print(f"  ✅ BF N=32 median={bf_32['median_ms']:.3f}ms < 15ms target")
        else:
            print(f"  ❌ BF N=32 median={bf_32['median_ms']:.3f}ms EXCEEDS 15ms target")
            all_pass = False
    else:
        print("  ⏭  BF N=32 not tested (quick mode)")

    # Grover functional check
    if grover_results:
        g4 = next((r for r in grover_results if r["n_nodes"] == 4), None)
        if g4:
            print(f"  ✅ Grover N=4 functional: mean={g4['mean_ms']:.3f}ms, qubits={g4['n_qubits']}")
    else:
        print("  ⏭  Grover not tested")

    # Summary
    print(f"\n  CSV saved to: {csv_path}")
    if all_pass:
        print("  🟢 ALL BENCHMARKS PASS PRODUCTION TARGETS")
    else:
        print("  🔴 SOME BENCHMARKS EXCEED PRODUCTION TARGETS")

    print("=" * 72)


if __name__ == "__main__":
    main()
