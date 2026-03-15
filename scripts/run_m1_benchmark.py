#!/usr/bin/env python3
"""
Automated benchmark runner for Phase 1 (Module 1: C++ LOB Engine).
Executes Siege inside the hqt-lob container and parses the output,
saving the results to the 'benchmark_runs' table in PostgreSQL.
"""
import subprocess
import psycopg
import os
import re

PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD', 'hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":5432/{os.getenv('POSTGRES_DB', 'hqt')}"
)

def run_siege():
    print("Running Siege benchmark against C++ LOB engine (target: > 100,000 QPS)...")
    cmd = [
        "docker", "exec", "hqt-lob",
        "siege", "-c", "200", "-t", "30S", "-f", "/app/urls.txt"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    out = result.stdout + result.stderr
    print(out)
    return out

def parse_and_save(output: str):
    """Parse Siege output and save to benchmark_runs."""
    # Example Siege output to parse:
    # Transactions:                  3000000 hits
    # Availability:                 100.00 %
    # Elapsed time:                  29.50 secs
    # Data transferred:               0.00 MB
    # Response time:                  0.00 secs
    # Transaction rate:          101694.92 trans/sec
    # Throughput:                     0.00 MB/sec
    # Concurrency:                  190.50
    # Successful transactions:     3000000
    # Failed transactions:               0
    # Longest transaction:            0.05
    # Shortest transaction:           0.00
    
    trans_match = re.search(r"Transactions:\s+(\d+)\s+hits", output)
    elapsed_match = re.search(r"Elapsed time:\s+([\d.]+)\s+secs", output)
    rate_match = re.search(r"Transaction rate:\s+([\d.]+)\s+trans/sec", output)
    succ_match = re.search(r"Successful transactions:\s+(\d+)", output)
    fail_match = re.search(r"Failed transactions:\s+(\d+)", output)
    long_t_match = re.search(r"Longest transaction:\s+([\d.]+)", output)
    
    if not all([trans_match, elapsed_match, rate_match, succ_match, fail_match, long_t_match]):
        print("Failed to parse siege output.")
        return
        
    total_reqs = int(trans_match.group(1))
    duration_sec = int(float(elapsed_match.group(1)))
    qps = float(rate_match.group(1))
    succ = int(succ_match.group(1))
    failed = int(fail_match.group(1))
    p99 = float(long_t_match.group(1)) * 1000 # Approximation from longest transaction
    
    print(f"Parsed: {qps} QPS over {duration_sec}s ({succ} successful, {failed} failed)")
    
    if qps < 100000:
        print(f"WARNING: QPS {qps} is below target 100,000!")
    else:
        print("SUCCESS: QPS target met.")
        
    with psycopg.connect(PG_DSN, autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO benchmark_runs (tool, target_endpoint, duration_sec, concurrent_users,
                                        total_requests, successful_reqs, failed_reqs,
                                        peak_qps, avg_latency_ms, p99_latency_ms, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                "siege",
                "http://127.0.0.1:8001/lob/order",
                duration_sec,
                200,
                total_reqs,
                succ,
                failed,
                qps,
                (200 / qps) * 1000 if qps > 0 else 0, # Little's Law approximation 
                p99,
                f"M1 C++ Rewrite benchmark. Target > 100,000 QPS. Result: {qps:.1f} QPS"
            ),
        )
    print("Benchmark results saved to database.")


if __name__ == "__main__":
    output = run_siege()
    parse_and_save(output)
