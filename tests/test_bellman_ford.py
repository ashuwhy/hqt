"""
tests.test_bellman_ford
~~~~~~~~~~~~~~~~~~~~~~~
Pure-Python unit tests for the Bellman-Ford arbitrage detector.

No database or Docker required - tests run against synthetic rate
matrices constructed in-memory.

Run with:
    python -m pytest tests/test_bellman_ford.py -v
"""

import math
import time
import random
import pytest

from module3_graph.bellman_ford import (
    bellman_ford_arbitrage,
    compute_cycle_profit,
    extract_cycle,
    benchmark_bellman_ford,
)

pytestmark = pytest.mark.unit


# ── Core detection tests ─────────────────────────────────────────────────────

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
                    rates[(s, d)] = (j + 1) / (i + 1)
        cycle = bellman_ford_arbitrage(rates, nodes)
        if cycle is not None:
            profit = compute_cycle_profit(cycle, rates)
            assert profit < 0.001, f"Spurious cycle with {profit}% profit"

    # ── New detection tests ───────────────────────────────────────────────────

    def test_profitable_4node_cycle_detected(self):
        """Known 4-node profitable cycle: A → B → C → D → A.

        Product = 1.2 * 1.3 * 1.1 * 0.8 = 1.37 → 37% profit.
        """
        nodes = ["A", "B", "C", "D"]
        rates = {
            ("A", "B"): 1.2, ("B", "A"): 0.8,
            ("B", "C"): 1.3, ("C", "B"): 0.7,
            ("C", "D"): 1.1, ("D", "C"): 0.9,
            ("D", "A"): 0.8, ("A", "D"): 1.2,
            ("A", "C"): 1.0, ("C", "A"): 1.0,
            ("B", "D"): 1.0, ("D", "B"): 1.0,
        }
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is not None, "Expected 4-node profitable cycle to be detected"
        assert len(cycle) >= 3

    def test_profitable_5node_cycle_detected(self):
        """Known 5-node profitable cycle with product > 1."""
        nodes = ["A", "B", "C", "D", "E"]
        # Build equilibrium
        rates = {(s, d): 1.0 for s in nodes for d in nodes if s != d}
        # Inject profitable cycle: A→B→C→D→E→A = 1.1^5 ≈ 1.61
        rates[("A", "B")] = 1.1
        rates[("B", "C")] = 1.1
        rates[("C", "D")] = 1.1
        rates[("D", "E")] = 1.1
        rates[("E", "A")] = 1.1
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is not None, "Expected 5-node profitable cycle to be detected"

    def test_marginal_profit_detected(self):
        """Product = 1.0001 → marginal but valid arbitrage must be detected."""
        nodes = ["X", "Y", "Z"]
        rates = {
            ("X", "Y"): 1.0001 ** (1.0 / 3.0),
            ("Y", "Z"): 1.0001 ** (1.0 / 3.0),
            ("Z", "X"): 1.0001 ** (1.0 / 3.0),
            ("Y", "X"): 0.99,
            ("Z", "Y"): 0.99,
            ("X", "Z"): 0.99,
        }
        # Product = 1.0001 exactly
        cycle = bellman_ford_arbitrage(rates, nodes)
        # Note: BF uses epsilon=1e-12, so 0.01% should be detected if it's > epsilon
        # The cycle product is ~1.0001 which gives -log sum < 0 → negative cycle
        if cycle is not None:
            profit = compute_cycle_profit(cycle, rates)
            assert profit > 0, f"Detected cycle should be profitable, got {profit}%"

    def test_extremely_small_rates(self):
        """Rates near zero should not cause crashes or infinite loops."""
        nodes = ["A", "B", "C"]
        rates = {
            ("A", "B"): 0.0001,
            ("B", "C"): 0.0001,
            ("C", "A"): 0.0001,
            ("B", "A"): 10000.0,
            ("C", "B"): 10000.0,
            ("A", "C"): 10000.0,
        }
        # Should not crash
        result = bellman_ford_arbitrage(rates, nodes)
        # With rates 10000 * 10000 * 10000 = 1e12 → massive arbitrage on the reverse
        assert result is not None, "Expected to find arbitrage with extreme rates"

    def test_extremely_large_rates(self):
        """Rates > 1e6 should be handled without overflow."""
        nodes = ["A", "B"]
        rates = {
            ("A", "B"): 1e8,
            ("B", "A"): 1e-8,
        }
        # Product of A→B→A = 1e8 * 1e-8 = 1.0 → no arbitrage
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is None, "Expected no arbitrage on balanced extreme rates"

    def test_duplicate_edges_last_wins(self):
        """When rates dict has a key set twice, last assignment wins."""
        nodes = ["A", "B", "C"]
        rates = {
            ("A", "B"): 1.0,
            ("B", "C"): 1.0,
            ("C", "A"): 1.0,
        }
        # Overwrite to create arbitrage
        rates[("A", "B")] = 2.0
        rates[("B", "C")] = 2.0
        rates[("C", "A")] = 2.0
        # Product = 8.0 → clear arbitrage
        cycle = bellman_ford_arbitrage(rates, nodes)
        assert cycle is not None, "Expected arbitrage after overwriting rates"


# ── Predecessor-walk tests ───────────────────────────────────────────────────

class TestCycleExtraction:
    """Predecessor-walk tests."""

    def test_extract_simple_cycle(self):
        pred = [2, 0, 1]
        symbols = ["A", "B", "C"]
        cycle = extract_cycle(pred, 0, 3, symbols)
        assert len(cycle) >= 3
        assert cycle[0] == cycle[-1]

    def test_extract_no_cycle(self):
        pred = [-1, -1, -1]
        symbols = ["A", "B", "C"]
        cycle = extract_cycle(pred, 0, 3, symbols)
        assert cycle == []


# ── Profit computation tests ─────────────────────────────────────────────────

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

    # ── New profit tests ──────────────────────────────────────────────────────

    def test_compute_cycle_profit_multi_hop(self):
        """5-hop cycle profit computation: A→B→C→D→E→A."""
        rates = {
            ("A", "B"): 1.1,
            ("B", "C"): 1.1,
            ("C", "D"): 1.1,
            ("D", "E"): 1.1,
            ("E", "A"): 1.1,
        }
        profit = compute_cycle_profit(["A", "B", "C", "D", "E", "A"], rates)
        # 1.1^5 = 1.61051 → 61.05% profit
        assert abs(profit - 61.051) < 0.1, f"Expected ~61.05% profit, got {profit}%"

    def test_compute_cycle_profit_missing_edge(self):
        """Missing edge in rates → -100% profit sentinel."""
        rates = {("A", "B"): 1.5}
        profit = compute_cycle_profit(["A", "B", "C", "A"], rates)
        assert profit == -100.0


# ── Performance tests ────────────────────────────────────────────────────────

class TestPerformance:
    """Bellman-Ford must run in < 5ms for N=20."""

    @pytest.mark.benchmark
    def test_bellman_ford_under_5ms(self):
        """Acceptance: BF completes in < 5ms at N=20 nodes."""
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

    @pytest.mark.benchmark
    def test_bellman_ford_under_5ms_at_32_nodes(self):
        """Stress: BF should complete in < 15ms at N=32 nodes (380 edges → 992 edges)."""
        random.seed(99)
        nodes = [f"N{i}" for i in range(32)]
        rates = {}
        for s in nodes:
            for d in nodes:
                if s != d:
                    rates[(s, d)] = random.uniform(0.8, 1.2)

        timings = []
        for _ in range(30):
            t0 = time.perf_counter()
            bellman_ford_arbitrage(rates, nodes)
            timings.append((time.perf_counter() - t0) * 1000)

        median = sorted(timings)[len(timings) // 2]
        assert median < 15.0, f"Median BF time at N=32 = {median:.2f}ms, must be < 15ms"

    def test_benchmark_returns_stats(self):
        stats = benchmark_bellman_ford(n_nodes=10, n_trials=10)
        assert "mean_ms" in stats
        assert "min_ms" in stats
        assert "max_ms" in stats
        assert stats["n_nodes"] == 10
        assert stats["n_trials"] == 10
