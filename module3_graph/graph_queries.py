"""
module3_graph.graph_queries
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Cypher query helpers executed through Apache AGE.

All functions accept a psycopg ``Connection`` (with AGE loaded) and
return plain Python data structures.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

logger = logging.getLogger(__name__)


def _cypher(conn: psycopg.Connection, query: str, columns: str = "v agtype") -> list[tuple]:
    """Execute a Cypher query via AGE and return rows."""
    sql = f"SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ {query} $cypher$) AS ({columns});"
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, \"$user\", public;")
        cur.execute(sql)
        return cur.fetchall()


def _strip(val: Any) -> str:
    """Strip AGE agtype quoting from a value."""
    return str(val).strip('"')


# ── Public query functions ───────────────────────────────────────────────────

def find_3hop_arbitrage_cycles(conn: psycopg.Connection, from_symbol: str) -> list[dict]:
    """Find all profitable directed 3-hop cycles starting from *from_symbol*.

    A cycle is profitable when the product of bid rates along the path > 1.
    """
    query = (
        f"MATCH (a:Asset {{symbol: '{from_symbol}'}})"
        f"-[r1:EXCHANGE]->(b:Asset)"
        f"-[r2:EXCHANGE]->(c:Asset)"
        f"-[r3:EXCHANGE]->(a) "
        f"WHERE a <> b AND b <> c AND a <> c "
        f"RETURN a.symbol, b.symbol, c.symbol, r1.bid, r2.bid, r3.bid"
    )
    columns = "a_sym agtype, b_sym agtype, c_sym agtype, r1_bid agtype, r2_bid agtype, r3_bid agtype"
    rows = _cypher(conn, query, columns)

    cycles = []
    for row in rows:
        a = _strip(row[0])
        b = _strip(row[1])
        c = _strip(row[2])
        try:
            r1 = float(str(row[3]))
            r2 = float(str(row[4]))
            r3 = float(str(row[5]))
        except (ValueError, TypeError):
            continue

        product = r1 * r2 * r3
        profit_pct = (product - 1.0) * 100
        if profit_pct > 0:
            cycles.append({
                "path": [a, b, c, a],
                "rates": [r1, r2, r3],
                "product": round(product, 8),
                "profit_pct": round(profit_pct, 6),
            })

    # Sort by profit descending
    cycles.sort(key=lambda c: c["profit_pct"], reverse=True)
    return cycles


def find_shortest_path(conn: psycopg.Connection, from_sym: str, to_sym: str) -> dict | None:
    """Find the most profitable single-hop or multi-hop exchange route.

    Uses the bid rate product to determine best path.
    Returns the path with highest rate product (= most profitable).
    """
    # Try direct edge first
    direct_q = (
        f"MATCH (a:Asset {{symbol: '{from_sym}'}})-[r:EXCHANGE]->(b:Asset {{symbol: '{to_sym}'}}) "
        f"RETURN r.bid"
    )
    direct = _cypher(conn, direct_q)
    best: dict | None = None

    if direct:
        try:
            rate = float(str(direct[0][0]))
            best = {"path": [from_sym, to_sym], "rate_product": rate, "hops": 1}
        except (ValueError, TypeError):
            pass

    # Try 2-hop
    hop2_q = (
        f"MATCH (a:Asset {{symbol: '{from_sym}'}})-[r1:EXCHANGE]->(m:Asset)-[r2:EXCHANGE]->(b:Asset {{symbol: '{to_sym}'}}) "
        f"WHERE m.symbol <> '{from_sym}' AND m.symbol <> '{to_sym}' "
        f"RETURN m.symbol, r1.bid, r2.bid"
    )
    hop2_cols = "mid agtype, r1 agtype, r2 agtype"
    for row in _cypher(conn, hop2_q, hop2_cols):
        try:
            mid = _strip(row[0])
            r1 = float(str(row[1]))
            r2 = float(str(row[2]))
            product = r1 * r2
            if best is None or product > best["rate_product"]:
                best = {"path": [from_sym, mid, to_sym], "rate_product": round(product, 8), "hops": 2}
        except (ValueError, TypeError):
            continue

    return best


def find_high_spread_edges(conn: psycopg.Connection, threshold: float) -> list[dict]:
    """Return all EXCHANGE edges where ``spread > threshold``."""
    query = (
        f"MATCH (a:Asset)-[r:EXCHANGE]->(b:Asset) "
        f"WHERE r.spread > {threshold} "
        f"RETURN a.symbol, b.symbol, r.bid, r.ask, r.spread"
    )
    columns = "src agtype, dst agtype, bid agtype, ask agtype, spread agtype"
    rows = _cypher(conn, query, columns)

    results = []
    for row in rows:
        try:
            results.append({
                "src": _strip(row[0]),
                "dst": _strip(row[1]),
                "bid": float(str(row[2])),
                "ask": float(str(row[3])),
                "spread": float(str(row[4])),
            })
        except (ValueError, TypeError):
            continue

    results.sort(key=lambda e: e["spread"], reverse=True)
    return results


def crypto_subgraph(conn: psycopg.Connection) -> dict:
    """Return the subgraph of crypto-only Asset nodes and their EXCHANGE edges."""
    node_q = "MATCH (a:Asset {asset_type: 'crypto'}) RETURN a.symbol"
    node_rows = _cypher(conn, node_q)
    nodes = [_strip(r[0]) for r in node_rows]

    edge_q = (
        "MATCH (a:Asset {asset_type: 'crypto'})-[r:EXCHANGE]->(b:Asset {asset_type: 'crypto'}) "
        "RETURN a.symbol, b.symbol, r.bid, r.ask"
    )
    edge_cols = "src agtype, dst agtype, bid agtype, ask agtype"
    edge_rows = _cypher(conn, edge_q, edge_cols)

    edges = []
    for row in edge_rows:
        try:
            edges.append({
                "src": _strip(row[0]),
                "dst": _strip(row[1]),
                "bid": float(str(row[2])),
                "ask": float(str(row[3])),
            })
        except (ValueError, TypeError):
            continue

    return {"nodes": nodes, "edges": edges}
