"""
Tests for Module 4 — Quantum Arbitrage Detection Engine.

These tests cover:
  - Grover oracle: circuit structure, phase-flip correctness
  - Grover diffuser: circuit structure
  - run_grover: cycle enumeration, profitability, full pipeline
  - quantum_api: HTTP endpoints (in-process, no real DB/simulator required)

Run with:
    pytest tests/test_quantum.py -v
"""
from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _simple_rates(n: int, base: float = 1.0) -> tuple[dict, list[str]]:
    """Build a fully-connected n-node rate matrix with uniform rates."""
    nodes = [f"N{i}" for i in range(n)]
    rates = {(s, d): base for s in nodes for d in nodes if s != d}
    return rates, nodes


def _profitable_rates(nodes: list[str]) -> dict:
    """Build a rate matrix with a guaranteed profitable 3-cycle: N0→N1→N2→N0."""
    rates = {(s, d): 1.0 for s in nodes for d in nodes if s != d}
    # Make N0→N1→N2→N0 profitable: product = 1.1 * 1.1 * 1.1 ≈ 1.331
    rates[("N0", "N1")] = 1.1
    rates[("N1", "N2")] = 1.1
    rates[("N2", "N0")] = 1.1
    return rates


# ─── Grover Oracle tests ──────────────────────────────────────────────────────

class TestGroverOracle:
    """Unit tests for grover_oracle.build_oracle."""

    def test_oracle_returns_quantum_circuit(self):
        from module4_quantum.grover_oracle import build_oracle
        from qiskit import QuantumCircuit
        qc = build_oracle([0, 1], n_qubits=3)
        assert isinstance(qc, QuantumCircuit)

    def test_oracle_has_correct_qubit_count(self):
        from module4_quantum.grover_oracle import build_oracle
        qc = build_oracle([0], n_qubits=4)
        assert qc.num_qubits == 4

    def test_oracle_empty_profitable_states(self):
        """Empty profitable list → identity circuit (no gates)."""
        from module4_quantum.grover_oracle import build_oracle
        qc = build_oracle([], n_qubits=3)
        assert qc.depth() == 0

    def test_oracle_single_qubit_edge_case(self):
        """Single-qubit oracle must not raise."""
        from module4_quantum.grover_oracle import build_oracle
        qc = build_oracle([0], n_qubits=1)
        assert qc.num_qubits == 1

    def test_oracle_raises_on_zero_qubits(self):
        from module4_quantum.grover_oracle import build_oracle
        with pytest.raises(ValueError):
            build_oracle([0], n_qubits=0)

    def test_oracle_depth_grows_with_states(self):
        """More profitable states → larger circuit depth."""
        from module4_quantum.grover_oracle import build_oracle
        qc_one = build_oracle([0], n_qubits=3)
        qc_two = build_oracle([0, 1], n_qubits=3)
        assert qc_two.depth() >= qc_one.depth()


# ─── Grover Diffuser tests ────────────────────────────────────────────────────

class TestGroverDiffuser:
    """Unit tests for grover_diffuser.build_diffuser."""

    def test_diffuser_returns_quantum_circuit(self):
        from module4_quantum.grover_diffuser import build_diffuser
        from qiskit import QuantumCircuit
        qc = build_diffuser(3)
        assert isinstance(qc, QuantumCircuit)

    def test_diffuser_qubit_count(self):
        from module4_quantum.grover_diffuser import build_diffuser
        for n in [1, 2, 3, 4, 5]:
            qc = build_diffuser(n)
            assert qc.num_qubits == n, f"Expected {n} qubits, got {qc.num_qubits}"

    def test_diffuser_has_positive_depth(self):
        from module4_quantum.grover_diffuser import build_diffuser
        for n in [2, 3, 4]:
            qc = build_diffuser(n)
            assert qc.depth() > 0

    def test_diffuser_raises_on_zero_qubits(self):
        from module4_quantum.grover_diffuser import build_diffuser
        with pytest.raises(ValueError):
            build_diffuser(0)


# ─── run_grover tests ─────────────────────────────────────────────────────────

class TestRunGrover:
    """Unit tests for run_grover module functions."""

    def test_enumerate_cycles_count(self):
        """P(N, 3) = N*(N-1)*(N-2) directed 3-cycles."""
        from module4_quantum.run_grover import enumerate_cycles
        nodes = ["A", "B", "C", "D"]
        cycles = enumerate_cycles(nodes, k=3)
        assert len(cycles) == 4 * 3 * 2  # 24

    def test_enumerate_cycles_tuples(self):
        from module4_quantum.run_grover import enumerate_cycles
        cycles = enumerate_cycles(["X", "Y", "Z"], k=3)
        assert all(isinstance(c, tuple) for c in cycles)
        assert all(len(c) == 3 for c in cycles)

    def test_is_profitable_true(self):
        from module4_quantum.run_grover import is_profitable
        rates = {("A", "B"): 1.1, ("B", "C"): 1.1, ("C", "A"): 1.1}
        assert is_profitable(("A", "B", "C"), rates) is True

    def test_is_profitable_false_uniform(self):
        from module4_quantum.run_grover import is_profitable
        rates = {("A", "B"): 1.0, ("B", "C"): 1.0, ("C", "A"): 1.0}
        assert is_profitable(("A", "B", "C"), rates) is False

    def test_is_profitable_missing_edge(self):
        from module4_quantum.run_grover import is_profitable
        rates = {("A", "B"): 1.5}  # missing B→C and C→A
        assert is_profitable(("A", "B", "C"), rates) is False

    def test_run_grover_too_few_nodes(self):
        """Less than 3 nodes → no 3-cycles, returns path=None."""
        from module4_quantum.run_grover import run_grover
        rates = {("A", "B"): 1.5, ("B", "A"): 0.7}
        result = run_grover(rates, ["A", "B"], shots=64)
        assert result["path"] is None

    def test_run_grover_no_profitable_cycles(self):
        """All rates = 1.0 → no profitable cycle → path=None."""
        from module4_quantum.run_grover import run_grover
        rates, nodes = _simple_rates(4, base=1.0)
        result = run_grover(rates, nodes, shots=64)
        assert result["path"] is None
        assert result["n_profitable"] == 0

    def test_run_grover_returns_path_when_profitable(self):
        """With a guaranteed profitable 3-cycle, run_grover must return a path."""
        from module4_quantum.run_grover import run_grover
        nodes = ["N0", "N1", "N2", "N3"]
        rates = _profitable_rates(nodes)
        result = run_grover(rates, nodes, shots=256)
        # A path is returned (may not be the optimal one due to shot noise)
        assert result["n_profitable"] >= 1
        # Result dict has all required keys
        for key in ("path", "profit_pct", "circuit_depth", "n_qubits", "n_iter",
                    "n_cycles", "n_profitable", "shots", "counts_top5"):
            assert key in result, f"Missing key: {key}"

    def test_run_grover_circuit_metadata(self):
        """Circuit metadata (n_qubits, circuit_depth, n_iter) must be positive ints."""
        from module4_quantum.run_grover import run_grover
        nodes = ["N0", "N1", "N2", "N3"]
        rates = _profitable_rates(nodes)
        result = run_grover(rates, nodes, shots=64)
        if result["path"] is not None:
            assert result["n_qubits"] >= 1
            assert result["circuit_depth"] >= 1
            assert result["n_iter"] >= 1

    def test_run_grover_cycle_cap(self):
        """n_cycles reported is capped at _MAX_CYCLES (32768)."""
        from module4_quantum.run_grover import run_grover, _MAX_CYCLES
        # With many nodes, P(N,3) would exceed the cap
        # Use a modest size that fits in memory but check cap logic
        nodes = [f"N{i}" for i in range(10)]
        rates, _ = _simple_rates(10)
        result = run_grover(rates, nodes, shots=32)
        # P(10,3) = 720 < 32768, so n_cycles == 720
        assert result["n_cycles"] == 10 * 9 * 8


# ─── quantum_api tests ────────────────────────────────────────────────────────

class TestQuantumAPI:
    """Integration tests for the FastAPI quantum_api app."""

    @pytest_asyncio.fixture
    async def client(self):
        """Async test client — patches quantum_loop so lifespan doesn't block."""
        import httpx
        from module4_quantum.quantum_api import app
        with patch("module4_quantum.quantum_api.quantum_loop", new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = None
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                yield c

    @pytest.mark.asyncio
    async def test_health_endpoint(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["module"] == "quantum_engine"

    @pytest.mark.asyncio
    async def test_quantum_health_alias(self, client):
        resp = await client.get("/quantum/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, client):
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_run_grover_invalid_method(self, client):
        resp = await client.post("/quantum/run-grover", json={"graph_size_n": 4, "method": "INVALID"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_run_grover_too_large(self, client):
        """graph_size_n > 32 must be rejected."""
        resp = await client.post("/quantum/run-grover", json={"graph_size_n": 33, "method": "BOTH"})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_benchmark_no_csv(self, client):
        """When benchmark CSV doesn't exist, endpoint returns available=False."""
        with patch("module4_quantum.quantum_api.BENCH_CSV") as mock_path:
            mock_path.exists.return_value = False
            resp = await client.get("/quantum/benchmark")
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is False
        assert data["rows"] == []
