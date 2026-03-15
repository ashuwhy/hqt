"""
tests.test_bellman_ford
~~~~~~~~~~~~~~~~~~~~~~~
Pure-Python unit tests for the Bellman-Ford arbitrage detector.

No database or Docker required — tests run against synthetic rate
matrices constructed in-memory. The algorithm itself is production
code; only the INPUT is synthetic for testing purposes.
"""

import math
import time
import pytest

from module3_graph.bellman_ford import (
    bellman_ford_arbitrage,
    compute_cycle_profit,
    extract_cycle,
    benchmark_bellman_ford,
)


class TestBellmanFordDetection:
    """Core negative-cycle detection tests."""

    def test_profitable_3node_cycle_detected(self):
        """Known 3-node profitable cycle must be detected.

        Cycle: A → B → C → A
        Rates: A→B = 2.0, B→C = 3.0, C→A = 0.2
        Product = 2.0 * 3.0 * 0.2 = 1.2 → 20% profit
        """
        nodes = ["A", "B", "C"]
        rates = {
            ("A", "B"): 2.0,
            ("B", "C"): 3.0,
            ("C", "A"): 0.2,
            ("B", "A"): 0.5,
            ("C", "B"): 0.33,
            ("A", "C"): 5.0,
        }
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is not None, "Expected a profitable cycle to be detected"
        assert len(cycle) >= 3, f"Cycle must have ≥3 nodes, got {cycle}"

    def test_no_arbitrage_returns_none(self):
        """Equilibrium rates with no negative cycle must return None.

        All rates are inverses: A→B = 2.0, B→A = 0.5 → product = 1.0
        """
        nodes = ["A", "B", "C"]
        rates = {
            ("A", "B"): 2.0,
            ("B", "A"): 0.5,
            ("B", "C"): 3.0,
            ("C", "B"): 1.0 / 3.0,
            ("A", "C"): 6.0,
            ("C", "A"): 1.0 / 6.0,
        }
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is None, f"Expected None for equilibrium rates, got {cycle}"

    def test_empty_graph_returns_none(self):
        assert bellman_ford_arbitrage({}, []) is None
        assert bellman_ford_arbitrage({}, ["A"]) is None
        assert bellman_ford_arbitrage({("A", "B"): 1.0}, []) is None

    def test_single_edge_returns_none(self):
        """Single edge cannot form a cycle."""
        nodes = ["A", "B"]
        rates = {("A", "B"): 1.5}
        assert bellman_ford_arbitrage(rates, nodes) is None

    def test_large_graph_no_arbitrage(self):
        """N=20 node graph with perfectly balanced rates → no arbitrage."""
        nodes = [f"N{i}" for i in range(20)]
        rates = {}
        for i, s in enumerate(nodes):
            for j, d in enumerate(nodes):
                if i != j:
                    # Rate from i→j = (j+1)/(i+1), inverse is (i+1)/(j+1)
                    rates[(s, d)] = (j + 1) / (i + 1)
        cycle = bellman_ford_arbitrage(rates, nodes)
        # With these exact inverse rates the product around any cycle = 1.0
        # The algorithm should not detect a cycle (within epsilon tolerance)
        # Note: due to floating point, might sometimes detect very marginal "cycles"
        if cycle is not None:
            profit = compute_cycle_profit(cycle, rates)
            assert profit < 0.001, f"Spurious cycle with {profit}% profit"


class TestCycleExtraction:
    """Predecessor-walk tests."""

    def test_extract_simple_cycle(self):
        # Predecessor chain: 0 ← 2 ← 1 ← 0 (cycle: 0 → 1 → 2 → 0)
        pred = [2, 0, 1]
        symbols = ["A", "B", "C"]
        cycle = extract_cycle(pred, 0, 3, symbols)
        assert len(cycle) >= 3
        # The cycle should be closed (first == last)
        assert cycle[0] == cycle[-1]

    def test_extract_no_cycle(self):
        pred = [-1, -1, -1]
        symbols = ["A", "B", "C"]
        cycle = extract_cycle(pred, 0, 3, symbols)
        assert cycle == []


class TestCycleProfit:
    """Profit computation tests."""

    def test_profitable_cycle(self):
        rates = {("A", "B"): 2.0, ("B", "C"): 3.0, ("C", "A"): 0.2}
        profit = compute_cycle_profit(["A", "B", "C", "A"], rates)
        assert abs(profit - 20.0) < 0.01, f"Expected ~20% profit, got {profit}%"

    def test_unprofitable_cycle(self):
        rates = {("A", "B"): 2.0, ("B", "C"): 0.3, ("C", "A"): 1.5}
        profit = compute_cycle_profit(["A", "B", "C", "A"], rates)
        assert profit < 0, f"Expected negative profit, got {profit}%"

    def test_breakeven_cycle(self):
        rates = {("A", "B"): 2.0, ("B", "A"): 0.5}
        profit = compute_cycle_profit(["A", "B", "A"], rates)
        assert abs(profit) < 0.01


class TestPerformance:
    """Bellman-Ford must run in < 5ms for N=20."""

    def test_bellman_ford_under_5ms(self):
        """Acceptance: BF completes in < 5ms at N=20 nodes."""
        import random
        random.seed(42)

        nodes = [f"N{i}" for i in range(20)]
        rates = {}
        for s in nodes:
            for d in nodes:
                if s != d:
                    rates[(s, d)] = random.uniform(0.8, 1.2)

        timings = []
        for _ in range(50):
            t0 = time.perf_counter()
            bellman_ford_arbitrage(rates, nodes)
            timings.append((time.perf_counter() - t0) * 1000)

        median = sorted(timings)[len(timings) // 2]
        assert median < 5.0, f"Median BF time = {median:.2f}ms, must be < 5ms"

    def test_benchmark_returns_stats(self):
        stats = benchmark_bellman_ford(n_nodes=10, n_trials=10)
        assert "mean_ms" in stats
        assert "min_ms" in stats
        assert "max_ms" in stats
        assert stats["n_nodes"] == 10
        assert stats["n_trials"] == 10
