#!/usr/bin/env python3
"""
Patch hqt_main.json with three new dashboard sections:
  - Hero row (6 stat panels, pinned at top)
  - Quantum Engine row (benchmark table + stats)
  - Security & Observability row

Run from repo root:
    python scripts/patch_grafana.py
"""
from __future__ import annotations

import json
from pathlib import Path

DASHBOARD = Path("module5_security/grafana_provisioning/dashboards/hqt_main.json")
HERO_SHIFT = 4   # rows added at top (1 row header + 3 stat height)
HERO_ROW_ID = 99


def already_patched(panels: list) -> bool:
    return any(p.get("id") == HERO_ROW_ID for p in panels)


def shift_panels(panels: list, dy: int) -> list:
    for p in panels:
        if "gridPos" in p:
            p["gridPos"]["y"] += dy
    return panels


# ── Hero row definition ────────────────────────────────────────────────────

def hero_row() -> list:
    def stat(pid, title, expr, ds_type, ds_uid, unit="short", color="#22d3ee", decimals=0, suffix=""):
        target = {
            "prometheus": {
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": expr,
                "instant": True,
                "refId": "A",
            },
            "postgres": {
                "datasource": {"type": "postgres", "uid": ds_uid},
                "rawSql": expr,
                "format": "table",
                "refId": "A",
            },
        }[ds_type]
        return {
            "id": pid,
            "type": "stat",
            "title": title,
            "datasource": {"type": ds_type, "uid": ds_uid},
            "targets": [target],
            "fieldConfig": {
                "defaults": {
                    "unit": unit,
                    "decimals": decimals,
                    "color": {"fixedColor": color, "mode": "fixed"},
                    "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
                    "mappings": [],
                },
                "overrides": [],
            },
            "options": {
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                "orientation": "auto",
                "textMode": "value_and_name",
                "colorMode": "background",
                "graphMode": "none",
                "justifyMode": "center",
            },
            "gridPos": {"x": pid % 10 * 4, "y": 1, "w": 4, "h": 3},  # overridden below
        }

    panels = [
        {
            "id": HERO_ROW_ID,
            "type": "row",
            "title": "⚡ System Overview",
            "collapsed": False,
            "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1},
        },
        {**stat(301, "LOB Throughput",
                'sum(rate(lob_orders_total[1m]))',
                "prometheus", "Prometheus", "reqps", "#22d3ee", 0),
         "gridPos": {"x": 0, "y": 1, "w": 4, "h": 3}},
        {**stat(302, "TimescaleDB Speedup",
                "SELECT ROUND(AVG(grover_mean_ms) / NULLIF(AVG(bf_mean_ms),0)) || '×' FROM benchmark_quantum_results WHERE benchmark_type='timescale'",
                "postgres", "TimescaleDB", "string", "#4ade80", 0),
         "gridPos": {"x": 4, "y": 1, "w": 4, "h": 3}},
        {**stat(303, "Arb Signals (24 h)",
                "SELECT COUNT(*) FROM arbitrage_signals WHERE ts > NOW()-INTERVAL '24 hours' AND method='CLASSICAL'",
                "postgres", "TimescaleDB", "short", "#f59e0b", 0),
         "gridPos": {"x": 8, "y": 1, "w": 4, "h": 3}},
        {**stat(304, "Grover Overhead @ N=32",
                "SELECT ROUND(grover_mean_ms / bf_mean_ms) || '×' FROM benchmark_quantum_results WHERE benchmark_type='quantum' AND n_nodes=32",
                "postgres", "TimescaleDB", "string", "#a78bfa", 0),
         "gridPos": {"x": 12, "y": 1, "w": 4, "h": 3}},
        {**stat(305, "SQL Injections Blocked (24 h)",
                "SELECT COUNT(*) FROM security_events WHERE event_type='SQL_INJECTION' AND ts > NOW()-INTERVAL '24 hours'",
                "postgres", "TimescaleDB", "short", "#f87171", 0),
         "gridPos": {"x": 16, "y": 1, "w": 4, "h": 3}},
        {**stat(306, "Services Up",
                'count(up == 1)',
                "prometheus", "Prometheus", "short", "#34d399", 0),
         "gridPos": {"x": 20, "y": 1, "w": 4, "h": 3}},
    ]
    return panels


# ── Quantum Engine row ────────────────────────────────────────────────────

def quantum_row(base_y: int) -> list:
    return [
        {
            "id": 400,
            "type": "row",
            "title": "🔬 Quantum Engine — Classical vs Quantum Complexity",
            "collapsed": False,
            "gridPos": {"x": 0, "y": base_y, "w": 24, "h": 1},
        },
        # Q1 — benchmark table
        {
            "id": 401,
            "type": "table",
            "title": "BF vs Grover Benchmark (N = 4 → 32)",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": (
                    "SELECT n_nodes AS \"N\", "
                    "bf_mean_ms AS \"BF (ms)\", "
                    "grover_mean_ms AS \"Grover (ms)\", "
                    "ROUND(grover_mean_ms / NULLIF(bf_mean_ms,0)) AS \"Ratio\" "
                    "FROM benchmark_quantum_results "
                    "WHERE benchmark_type='quantum' "
                    "ORDER BY n_nodes"
                ),
                "format": "table",
                "refId": "A",
            }],
            "fieldConfig": {
                "defaults": {"custom": {"align": "center", "filterable": False}},
                "overrides": [
                    {"matcher": {"id": "byName", "options": "BF (ms)"},
                     "properties": [{"id": "color", "value": {"fixedColor": "#4ade80", "mode": "fixed"}},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                    {"matcher": {"id": "byName", "options": "Grover (ms)"},
                     "properties": [{"id": "color", "value": {"fixedColor": "#a78bfa", "mode": "fixed"}},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                    {"matcher": {"id": "byName", "options": "Ratio"},
                     "properties": [{"id": "color", "value": {"fixedColor": "#fb923c", "mode": "fixed"}},
                                    {"id": "custom.cellOptions", "value": {"type": "color-text"}}]},
                ],
            },
            "options": {"showHeader": True, "sortBy": [{"displayName": "N", "desc": False}]},
            "gridPos": {"x": 0, "y": base_y + 1, "w": 14, "h": 9},
        },
        # Q2 — BF p99 @ N=20
        {
            "id": 402,
            "type": "stat",
            "title": "BF p99 @ N=20 (production)",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT bf_p99_ms FROM benchmark_quantum_results WHERE benchmark_type='quantum' AND n_nodes=20",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "ms", "decimals": 2,
                "color": {"fixedColor": "#4ade80", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "#4ade80", "value": None}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 14, "y": base_y + 1, "w": 5, "h": 4},
        },
        # Q3 — circuit depth @ N=32
        {
            "id": 403,
            "type": "stat",
            "title": "Circuit Depth @ N=32",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT circuit_depth FROM benchmark_quantum_results WHERE benchmark_type='quantum' AND n_nodes=32",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                "color": {"fixedColor": "#a78bfa", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "#a78bfa", "value": None}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 19, "y": base_y + 1, "w": 5, "h": 4},
        },
        # Q4 — qubits @ N=32
        {
            "id": 404,
            "type": "stat",
            "title": "Qubits @ N=32",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT n_qubits FROM benchmark_quantum_results WHERE benchmark_type='quantum' AND n_nodes=32",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                "color": {"fixedColor": "#a78bfa", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "#a78bfa", "value": None}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 14, "y": base_y + 5, "w": 5, "h": 4},
        },
        # Q5 — Grover overhead @ N=32
        {
            "id": 405,
            "type": "stat",
            "title": "Grover Overhead @ N=32",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT ROUND(grover_mean_ms / bf_mean_ms) FROM benchmark_quantum_results WHERE benchmark_type='quantum' AND n_nodes=32",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                "displayName": "× slower than BF",
                "color": {"fixedColor": "#fb923c", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "#fb923c", "value": None}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 19, "y": base_y + 5, "w": 5, "h": 4},
        },
        # Q6 — explanatory text
        {
            "id": 406,
            "type": "text",
            "title": "",
            "options": {
                "mode": "markdown",
                "content": (
                    "**Why is Grover slower on AerSimulator?**  \n"
                    "AerSimulator computes the full 2ⁿ-element state vector classically, "
                    "producing **O(2ⁿ) overhead** per iteration. "
                    "On *real quantum hardware*, Grover's oracle query complexity is **O(√N)** — "
                    "a quadratic speedup over classical search. "
                    "The divergence between the two lines in the benchmark chart is the research result: "
                    "it isolates simulator overhead from the theoretical quantum advantage."
                ),
            },
            "gridPos": {"x": 0, "y": base_y + 10, "w": 24, "h": 3},
        },
    ]


# ── Security & Observability row ─────────────────────────────────────────

def security_row(base_y: int) -> list:
    return [
        {
            "id": 500,
            "type": "row",
            "title": "🛡️ Security & Observability",
            "collapsed": False,
            "gridPos": {"x": 0, "y": base_y, "w": 24, "h": 1},
        },
        # S1 — SQL injections
        {
            "id": 501,
            "type": "stat",
            "title": "SQL Injections Blocked (24 h)",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT COUNT(*) FROM security_events WHERE event_type='SQL_INJECTION' AND ts > NOW()-INTERVAL '24 hours'",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                "color": {"fixedColor": "#f87171", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "#f87171", "value": 1}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 0, "y": base_y + 1, "w": 6, "h": 5},
        },
        # S2 — rate limit hits
        {
            "id": 502,
            "type": "stat",
            "title": "Rate Limit Hits (24 h)",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": "SELECT COUNT(*) FROM security_events WHERE event_type='RATE_LIMIT' AND ts > NOW()-INTERVAL '24 hours'",
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "short", "decimals": 0,
                "color": {"fixedColor": "#fb923c", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "#fb923c", "value": 1}]}}},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "background", "graphMode": "none", "textMode": "value_and_name", "justifyMode": "center"},
            "gridPos": {"x": 6, "y": base_y + 1, "w": 6, "h": 5},
        },
        # S3 — proxy QPS
        {
            "id": 503,
            "type": "timeseries",
            "title": "Proxy QPS (orders/sec)",
            "datasource": {"type": "prometheus", "uid": "Prometheus"},
            "targets": [{
                "datasource": {"type": "prometheus", "uid": "Prometheus"},
                "expr": "sum(rate(lob_orders_total[1m]))",
                "legendFormat": "orders/sec",
                "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "reqps", "color": {"fixedColor": "#22d3ee", "mode": "fixed"}}},
            "options": {"tooltip": {"mode": "single"}, "legend": {"displayMode": "list", "placement": "bottom"}},
            "gridPos": {"x": 12, "y": base_y + 1, "w": 12, "h": 5},
        },
        # S4 — p99 latency
        {
            "id": 504,
            "type": "timeseries",
            "title": "p99 Order Latency (ms)",
            "datasource": {"type": "prometheus", "uid": "Prometheus"},
            "targets": [{
                "datasource": {"type": "prometheus", "uid": "Prometheus"},
                "expr": "histogram_quantile(0.99, sum(rate(lob_order_latency_ms_bucket[1m])) by (le))",
                "legendFormat": "p99 ms",
                "refId": "A",
            }],
            "fieldConfig": {"defaults": {"unit": "ms", "color": {"fixedColor": "#4ade80", "mode": "fixed"},
                "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 5}, {"color": "red", "value": 10}]}}},
            "options": {"tooltip": {"mode": "single"}, "legend": {"displayMode": "list", "placement": "bottom"}},
            "gridPos": {"x": 0, "y": base_y + 6, "w": 12, "h": 7},
        },
        # S5 — security events table
        {
            "id": 505,
            "type": "table",
            "title": "Recent Security Events",
            "datasource": {"type": "postgres", "uid": "TimescaleDB"},
            "targets": [{
                "datasource": {"type": "postgres", "uid": "TimescaleDB"},
                "rawSql": (
                    "SELECT to_char(ts, 'HH24:MI:SS') AS \"Time\", "
                    "event_type AS \"Event\", "
                    "client_ip AS \"Client IP\", "
                    "endpoint AS \"Endpoint\", "
                    "blocked AS \"Blocked\" "
                    "FROM security_events "
                    "ORDER BY ts DESC LIMIT 20"
                ),
                "format": "table", "refId": "A",
            }],
            "fieldConfig": {
                "defaults": {"custom": {"align": "left", "filterable": True}},
                "overrides": [
                    {"matcher": {"id": "byName", "options": "Event"},
                     "properties": [{"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                    {"id": "mappings", "value": [
                                        {"type": "value", "options": {"SQL_INJECTION": {"color": "#f87171", "index": 0}}},
                                        {"type": "value", "options": {"RATE_LIMIT": {"color": "#fb923c", "index": 1}}},
                                        {"type": "value", "options": {"AUTH_FAIL": {"color": "#facc15", "index": 2}}},
                                    ]}]},
                ],
            },
            "options": {"showHeader": True},
            "gridPos": {"x": 12, "y": base_y + 6, "w": 12, "h": 7},
        },
    ]


def main() -> None:
    data = json.loads(DASHBOARD.read_text())
    panels = data["panels"]

    if already_patched(panels):
        print("Dashboard already patched — nothing to do.")
        return

    # 1. Shift all existing panels down to make room for the hero row
    shift_panels(panels, HERO_SHIFT)

    # 2. Prepend hero panels
    panels = hero_row() + panels

    # 3. Find the new max y to append quantum + security rows
    max_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in panels)
    q_panels = quantum_row(max_y)
    panels.extend(q_panels)

    q_max_y = max(p["gridPos"]["y"] + p["gridPos"]["h"] for p in q_panels)
    panels.extend(security_row(q_max_y))

    data["panels"] = panels
    DASHBOARD.write_text(json.dumps(data, indent=2))
    print(f"Dashboard patched. Total panels: {len(panels)}")


if __name__ == "__main__":
    main()
