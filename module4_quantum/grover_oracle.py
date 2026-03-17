"""
module4_quantum.grover_oracle
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Build Grover's phase-flip oracle circuit.

For each profitable basis state the oracle:
  1. Applies X to every qubit whose bit is '0' (converts the state to all-1s)
  2. Applies H to the ancilla (last qubit)
  3. Applies MCX (multi-controlled-X) with all data qubits as controls and
     the ancilla as target — this achieves the phase flip via the H-MCX-H trick
  4. Applies H to the ancilla (undo)
  5. Un-applies the X gates from step 1
"""

from __future__ import annotations

import logging

from qiskit import QuantumCircuit, QuantumRegister

logger = logging.getLogger(__name__)


def build_oracle(profitable_states: list[int], n_qubits: int) -> QuantumCircuit:
    """Phase-flip all profitable basis states using multi-controlled-X.

    Parameters
    ----------
    profitable_states:
        List of integer indices (0-based) that correspond to profitable
        arbitrage cycles.  Each integer maps to one computational basis state.
    n_qubits:
        Total number of qubits in the circuit.  The last qubit (index
        ``n_qubits - 1``) is used as the ancilla / phase-kick qubit.

    Returns
    -------
    QuantumCircuit
        A ``n_qubits``-qubit circuit that applies a −1 phase to every basis
        state whose index is in ``profitable_states``.

    Algorithm (per profitable state s)
    ------------------------------------
    1. Represent *s* as an ``(n_qubits - 1)``-bit binary string (MSB first).
    2. Apply X to every data qubit whose corresponding bit is '0', so the
       target state maps to |11…1⟩ on the data register.
    3. Apply H to the ancilla to put it in |−⟩ = (|0⟩ − |1⟩)/√2.
    4. Apply MCX with controls = [0 … n_qubits-2], target = n_qubits-1.
       When all controls are |1⟩ the ancilla flips, but because it is in |−⟩
       the operation contributes a global −1 phase to that term — this is the
       standard phase-kickback trick.
    5. Apply H to the ancilla to restore it to |0⟩.
    6. Un-apply the X gates from step 2.
    """
    if n_qubits < 1:
        raise ValueError(f"n_qubits must be >= 1, got {n_qubits}")

    qr = QuantumRegister(n_qubits, "q")
    qc = QuantumCircuit(qr)

    # Data qubits are 0 … n_qubits-2; ancilla is n_qubits-1
    data_qubits = list(range(n_qubits - 1)) if n_qubits > 1 else []
    ancilla = n_qubits - 1
    n_data = n_qubits - 1  # number of bits used to index a cycle

    for state in profitable_states:
        if state < 0:
            continue

        if n_data == 0:
            # Single-qubit edge case: only state 0 exists; phase flip it
            # directly with a Z gate (no ancilla / data distinction).
            qc.z(0)
            continue

        # Step 1 — convert state → |11…1⟩ on data register
        # Binary representation, padded to n_data bits, MSB first
        bits = format(state, f"0{n_data}b")
        x_targets = [data_qubits[i] for i, b in enumerate(bits) if b == "0"]
        if x_targets:
            qc.x(x_targets)

        # Step 2 — put ancilla in |−⟩
        qc.h(ancilla)

        # Step 3 — MCX: controls = all data qubits, target = ancilla
        if data_qubits:
            qc.mcx(data_qubits, ancilla)
        else:
            # n_qubits == 1 handled above; this branch is unreachable
            qc.x(ancilla)

        # Step 4 — restore ancilla to |0⟩
        qc.h(ancilla)

        # Step 5 — undo the X gates
        if x_targets:
            qc.x(x_targets)

    logger.debug(
        "Oracle built: %d profitable states, %d qubits, depth=%d",
        len(profitable_states),
        n_qubits,
        qc.depth(),
    )
    return qc
