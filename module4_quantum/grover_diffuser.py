"""
module4_quantum.grover_diffuser
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Build Grover's diffusion operator (inversion about the average / uniform
superposition).

The diffuser implements the reflection  2|s⟩⟨s| − I  where
|s⟩ = H^⊗n |0…0⟩  is the uniform superposition over all 2^n basis states.

This amplifies the amplitude of the marked (profitable) states and suppresses
the amplitude of all unmarked states after each Grover iteration.
"""

from __future__ import annotations

import logging

from qiskit import QuantumCircuit, QuantumRegister

logger = logging.getLogger(__name__)


def build_diffuser(n_qubits: int) -> QuantumCircuit:
    """Inversion about the uniform superposition: 2|s⟩⟨s| − I.

    Parameters
    ----------
    n_qubits:
        Number of qubits.  Must be >= 1.

    Returns
    -------
    QuantumCircuit
        An ``n_qubits``-qubit circuit implementing the Grover diffusion
        operator.

    Algorithm
    ----------
    1. H all qubits  — map uniform superposition back to computational basis
    2. X all qubits  — flip so that only |0…0⟩ is |1…1⟩
    3. H on last qubit  — put it in |−⟩ for phase kickback
    4. MCX with controls = [0 … n_qubits-2], target = n_qubits-1
       — phase flip |1…1⟩  (i.e. the original |0…0⟩)
    5. H on last qubit  — restore last qubit
    6. X all qubits  — undo step 2
    7. H all qubits  — map back to superposition basis

    The net effect is:  every amplitude is reflected about the mean, which
    constructively amplifies the marked states.
    """
    if n_qubits < 1:
        raise ValueError(f"n_qubits must be >= 1, got {n_qubits}")

    qr = QuantumRegister(n_qubits, "q")
    qc = QuantumCircuit(qr)

    all_qubits = list(range(n_qubits))
    last = n_qubits - 1

    if n_qubits == 1:
        # Special case: single qubit diffuser is just a Z gate (up to global phase)
        qc.h(0)
        qc.z(0)
        qc.h(0)
        return qc

    # Step 1 — H all qubits
    qc.h(all_qubits)

    # Step 2 — X all qubits
    qc.x(all_qubits)

    # Step 3 — H on last qubit (ancilla for phase kickback)
    qc.h(last)

    # Step 4 — MCX: controls = all qubits except last, target = last
    control_qubits = list(range(n_qubits - 1))
    qc.mcx(control_qubits, last)

    # Step 5 — H on last qubit
    qc.h(last)

    # Step 6 — X all qubits
    qc.x(all_qubits)

    # Step 7 — H all qubits
    qc.h(all_qubits)

    logger.debug(
        "Diffuser built: %d qubits, depth=%d",
        n_qubits,
        qc.depth(),
    )
    return qc
