"""
module4_quantum.run_grover
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Full Grover's algorithm pipeline for arbitrage cycle detection.

Given a rate matrix (FX exchange rates between N assets), this module:
  1. Enumerates all P(N, 3) directed 3-cycles
  2. Identifies which cycles are profitable (product of rates > 1.0)
  3. Encodes them as a quantum search problem
  4. Runs Grover's algorithm on AerSimulator
  5. Returns the most-measured cycle as the detected arbitrage path

Complexity note:
  Classical enumeration is O(N^3).  Grover's algorithm on a real quantum device
  would provide an O(√N) query advantage for the *search* step.  However,
  AerSimulator computes the full state vector classically, so runtime grows
  exponentially with qubit count on this simulator.
"""

from __future__ import annotations

import logging
import math
from itertools import permutations

from qiskit import QuantumCircuit

from module4_quantum.grover_oracle import build_oracle
from module4_quantum.grover_diffuser import build_diffuser

logger = logging.getLogger(__name__)

# Cap to prevent RAM exhaustion: 2^15 = 32 768 states
_MAX_CYCLES = 32_768


# ─── Cycle enumeration ────────────────────────────────────────────────────────

def enumerate_cycles(nodes: list[str], k: int = 3) -> list[tuple[str, ...]]:
    """All P(N, k) directed k-cycles as tuples.

    Returns all ordered k-permutations of *nodes*.  Each tuple (a, b, c)
    represents the directed cycle  a → b → c → a.

    Parameters
    ----------
    nodes:
        List of asset/node symbols.
    k:
        Cycle length.  Defaults to 3 (triangular arbitrage).

    Returns
    -------
    list[tuple[str, ...]]
        All P(N, k) directed permutations.  For 3-cycles this is N*(N-1)*(N-2).
    """
    return list(permutations(nodes, k))


# ─── Profitability check ──────────────────────────────────────────────────────

def is_profitable(cycle: tuple[str, ...], rates: dict[tuple[str, str], float]) -> bool:
    """Return True if the product of rates along the cycle exceeds 1.0.

    The cycle is closed, so the edges traversed are:
        cycle[0]→cycle[1], cycle[1]→cycle[2], …, cycle[-1]→cycle[0]

    A product > 1.0 means converting 1 unit of cycle[0] and going around the
    cycle yields more than 1 unit back — i.e. a profitable arbitrage.

    Parameters
    ----------
    cycle:
        Tuple of node symbols, e.g. ('USD', 'EUR', 'GBP').
    rates:
        Dict mapping (src, dst) → exchange rate (float).

    Returns
    -------
    bool
        True if the cycle is profitable.
    """
    product = 1.0
    n = len(cycle)
    for i in range(n):
        src = cycle[i]
        dst = cycle[(i + 1) % n]
        rate = rates.get((src, dst))
        if rate is None or rate <= 0:
            return False
        product *= rate
    return product > 1.0


# ─── Profit percentage helper ─────────────────────────────────────────────────

def _cycle_profit_pct(cycle: tuple[str, ...], rates: dict[tuple[str, str], float]) -> float:
    """Compute the profit percentage for a closed cycle."""
    product = 1.0
    n = len(cycle)
    for i in range(n):
        src = cycle[i]
        dst = cycle[(i + 1) % n]
        rate = rates.get((src, dst), 0.0)
        if rate <= 0:
            return -100.0
        product *= rate
    return (product - 1.0) * 100.0


# ─── Grover pipeline ──────────────────────────────────────────────────────────

def run_grover(
    rates_matrix: dict[tuple[str, str], float],
    nodes: list[str],
    shots: int = 1024,
) -> dict:
    """Run Grover's algorithm to detect the most profitable arbitrage cycle.

    Parameters
    ----------
    rates_matrix:
        Dict mapping (src, dst) → float exchange rate.
    nodes:
        List of asset symbols that appear in the rate matrix.
    shots:
        Number of measurement shots for the AerSimulator.

    Returns
    -------
    dict
        Keys:
            path          — list[str] best detected cycle (closed), or None
            profit_pct    — float profit percentage
            circuit_depth — int Qiskit circuit depth
            n_qubits      — int qubits used
            n_iter        — int Grover iterations applied
            n_cycles      — int total cycles enumerated
            n_profitable  — int number of profitable cycles found
            shots         — int shots used
            counts_top5   — dict top-5 measurement outcomes (state → count)
    """
    base_result: dict = {
        "path": None,
        "profit_pct": 0.0,
        "circuit_depth": 0,
        "n_qubits": 0,
        "n_iter": 0,
        "n_cycles": 0,
        "n_profitable": 0,
        "shots": shots,
        "counts_top5": {},
    }

    if not nodes or len(nodes) < 3:
        logger.warning("run_grover: not enough nodes (%d) for 3-cycles", len(nodes))
        return base_result

    # ── Step 1: enumerate all directed 3-cycles ──────────────────────────────
    cycles = enumerate_cycles(nodes, k=3)
    n_cycles_total = len(cycles)

    # ── Step 4 (spec): cap to _MAX_CYCLES to avoid RAM exhaustion ────────────
    if len(cycles) > _MAX_CYCLES:
        logger.warning(
            "run_grover: capping cycles from %d to %d to avoid RAM exhaustion",
            len(cycles), _MAX_CYCLES,
        )
        cycles = cycles[:_MAX_CYCLES]

    n_cycles = len(cycles)
    base_result["n_cycles"] = n_cycles_total

    # ── Step 2: find profitable cycles ───────────────────────────────────────
    profitable_idxs = [
        i for i, c in enumerate(cycles)
        if is_profitable(c, rates_matrix)
    ]
    n_profitable = len(profitable_idxs)
    base_result["n_profitable"] = n_profitable

    if n_profitable == 0:
        logger.info("run_grover: no profitable cycles found in %d candidates", n_cycles)
        return base_result

    # ── Step 5: compute qubit count ───────────────────────────────────────────
    # n_data qubits hold the cycle index (can address 2^n_data states).
    # +1 dedicated ancilla qubit used by the oracle for phase-kickback.
    # build_oracle(state, n_qubits) uses the last qubit as ancilla, so
    # state indices must fit in n_qubits-1 = n_data bits.
    n_data = max(1, math.ceil(math.log2(n_cycles))) if n_cycles > 1 else 1
    n_qubits = n_data + 1  # total circuit width = data + 1 ancilla

    # ── Step 6: compute Grover iteration count ────────────────────────────────
    # n_iter = max(1, floor(π/4 * sqrt(N / M)))
    # where N = 2^n_data (data search space), M = n_profitable
    search_space = 2 ** n_data
    n_iter = max(
        1,
        int(math.pi / 4.0 * math.sqrt(search_space / max(n_profitable, 1))),
    )

    logger.info(
        "run_grover: nodes=%d cycles=%d profitable=%d n_data=%d n_qubits=%d n_iter=%d shots=%d",
        len(nodes), n_cycles, n_profitable, n_data, n_qubits, n_iter, shots,
    )

    # ── Step 7: build the circuit ─────────────────────────────────────────────
    qc = QuantumCircuit(n_qubits)

    # (a) Hadamard all qubits → uniform superposition
    qc.h(range(n_qubits))

    # (b) n_iter repetitions of oracle + diffuser
    oracle = build_oracle(profitable_idxs, n_qubits)
    diffuser = build_diffuser(n_qubits)

    for _ in range(n_iter):
        qc.compose(oracle, inplace=True)
        qc.compose(diffuser, inplace=True)

    # (c) Measure all qubits
    qc.measure_all()

    circuit_depth = qc.depth()

    # ── Step 8: run on AerSimulator ───────────────────────────────────────────
    from qiskit_aer import AerSimulator  # lazy import — heavy C++ extension
    simulator = AerSimulator()
    job = simulator.run(qc, shots=shots)
    result = job.result()
    counts: dict[str, int] = result.get_counts(qc)

    # ── Step 9: extract best state ────────────────────────────────────────────
    # Qiskit's measurement output is a bit-string of length n_qubits.
    # The ancilla qubit is the last qubit (index n_qubits-1), which appears
    # as the LEFTMOST character in Qiskit's big-endian string.
    # Strip the ancilla bit (leftmost char) to get the n_data-bit cycle index.
    best_state_str = max(counts, key=counts.get)  # type: ignore[arg-type]
    # Drop the ancilla bit (first character in big-endian Qiskit output)
    data_bits_str = best_state_str[1:] if len(best_state_str) > 1 else best_state_str
    best_state = int(data_bits_str, 2)

    # Map best_state to a cycle; clamp to valid index range
    best_state = best_state % n_cycles
    best_cycle = cycles[best_state]

    # Build the closed path list: (a, b, c) → [a, b, c, a]
    best_path = list(best_cycle) + [best_cycle[0]]
    profit_pct = _cycle_profit_pct(best_cycle, rates_matrix)

    # Top-5 counts for diagnostics
    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    counts_top5 = dict(sorted_counts[:5])

    return {
        "path": best_path,
        "profit_pct": round(profit_pct, 6),
        "circuit_depth": circuit_depth,
        "n_qubits": n_qubits,   # total circuit width (data + 1 ancilla)
        "n_iter": n_iter,
        "n_cycles": n_cycles_total,
        "n_profitable": n_profitable,
        "shots": shots,
        "counts_top5": counts_top5,
    }
