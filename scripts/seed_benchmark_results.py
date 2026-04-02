#!/usr/bin/env python3
"""
Seed benchmark_quantum_results from the two bench_out CSV files.
Run once after docker compose up:
    python scripts/seed_benchmark_results.py
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import psycopg

PG_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://hqt:hqt_secret@localhost:5432/hqt?sslmode=disable",
)

QUANTUM_CSV = Path(__file__).parent.parent / "module4_quantum/bench_out/benchmark_quantum.csv"
TIMESCALE_CSV = Path(__file__).parent.parent / "module2_timescale/bench_out/benchmark_timescale.csv"

INSERT_SQL = """
INSERT INTO benchmark_quantum_results
    (benchmark_type, n_nodes, bf_mean_ms, bf_p99_ms,
     grover_mean_ms, grover_p99_ms, n_qubits, circuit_depth, n_iter)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT DO NOTHING
"""

def seed_quantum(conn: psycopg.Connection) -> int:
    rows = 0
    with open(QUANTUM_CSV) as f:
        for row in csv.DictReader(f):
            conn.execute(INSERT_SQL, (
                "quantum",
                int(row["n_nodes"]),
                float(row["bf_mean_ms"]),
                float(row["bf_p99_ms"]),
                float(row["grover_mean_ms"]),
                float(row["grover_p99_ms"]),
                int(row["n_qubits"]),
                int(row["circuit_depth"]),
                int(row["n_iter"]),
            ))
            rows += 1
    return rows


def seed_timescale(conn: psycopg.Connection) -> int:
    """Each trial row becomes one timescale entry; n_nodes = trial number."""
    rows = 0
    with open(TIMESCALE_CSV) as f:
        for row in csv.DictReader(f):
            conn.execute(INSERT_SQL, (
                "timescale",
                int(row["trial"]),
                float(row["hypertable_ms"]),   # bf_mean_ms = hypertable query time
                None,                          # bf_p99_ms — not in timescale CSV
                float(row["plain_ms"]),        # grover_mean_ms = plain table query time
                None,                          # grover_p99_ms — not in timescale CSV
                None, None, None,
            ))
            rows += 1
    return rows


def main() -> None:
    print(f"Connecting to {PG_DSN[:40]}...")
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        q = seed_quantum(conn)
        t = seed_timescale(conn)
    print(f"Seeded {q} quantum rows, {t} timescale rows into benchmark_quantum_results.")


if __name__ == "__main__":
    main()
