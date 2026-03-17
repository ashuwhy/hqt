"""
tests.test_graph
~~~~~~~~~~~~~~~~
Integration and unit tests for the Module 3 Graph Arbitrage Engine.

Sections
--------
1. TestBellmanFordIntegration  — DB-backed Bellman-Ford tests (require AGE graph)
2. TestGraphQueries            — DB-backed Cypher query helpers
3. TestGraphAPI                — HTTP integration tests against the running graph-service
4. TestGraphInit               — DB-backed graph initialisation (idempotency, node/edge presence)

All DB tests use the ``db_conn`` session-scoped fixture from conftest.py.
All HTTP tests use the ``graph_client`` async fixture from conftest.py and are
marked ``@pytest.mark.integration`` + ``@pytest.mark.asyncio``.
"""

from __future__ import annotations

import pytest
import psycopg

from module3_graph.bellman_ford import (
    build_rate_matrix,
    bellman_ford_arbitrage,
    compute_cycle_profit,
    _insert_signal,
)
from module3_graph.graph_queries import (
    find_high_spread_edges,
    crypto_subgraph,
    find_shortest_path,
)
from module3_graph.graph_init import init_graph


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _graph_is_initialised(conn: psycopg.Connection) -> bool:
    """Return True if the AGE fx_graph exists and has Asset nodes."""
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset) RETURN count(a) $cypher$) AS (v agtype);"
            )
            row = cur.fetchone()
            return int(str(row[0])) > 0 if row else False
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: Bellman-Ford Integration (DB required)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestBellmanFordIntegration:
    """Integration tests for build_rate_matrix and bellman_ford_arbitrage."""

    def test_build_rate_matrix_returns_data(self, db_conn: psycopg.Connection):
        """build_rate_matrix must return a non-empty rate dict and ≥2 nodes."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)

        assert isinstance(rates, dict), "rates must be a dict"
        assert len(rates) > 0, "rates dict must not be empty"
        assert isinstance(nodes, list), "nodes must be a list"
        assert len(nodes) >= 2, f"Expected ≥2 nodes, got {len(nodes)}"

        for (src, dst), rate in rates.items():
            assert isinstance(rate, float), f"rate for ({src}, {dst}) must be float"
            assert rate > 0, f"rate for ({src}, {dst}) must be positive, got {rate}"

    def test_rate_matrix_node_consistency(self, db_conn: psycopg.Connection):
        """Every (src, dst) key in rates must have both src and dst in nodes."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)
        node_set = set(nodes)

        for (src, dst) in rates:
            assert src in node_set, f"src '{src}' not found in nodes list"
            assert dst in node_set, f"dst '{dst}' not found in nodes list"

    def test_run_detector_inserts_signal(self, db_conn: psycopg.Connection):
        """If BF finds a profitable cycle, _insert_signal must write a CLASSICAL row."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)
        if len(nodes) < 4:
            pytest.skip(f"Not enough nodes ({len(nodes)}) for a meaningful cycle check")

        cycle = bellman_ford_arbitrage(rates, nodes)
        if cycle is None:
            pytest.skip("No arbitrage cycle found — skipping insertion test")

        profit_pct = compute_cycle_profit(cycle, rates)
        if profit_pct <= 0:
            pytest.skip("Detected cycle is not profitable — skipping insertion test")

        _insert_signal(db_conn, cycle, profit_pct, 1.23, len(nodes))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM arbitrage_signals WHERE method = 'CLASSICAL';")
            row = cur.fetchone()

        assert row is not None
        assert int(row[0]) >= 1, f"Expected ≥1 CLASSICAL signal, got {int(row[0])}"

    def test_compute_cycle_profit_positive(self, db_conn: psycopg.Connection):
        """If BF detects a cycle, profit must be > 0."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)
        cycle = bellman_ford_arbitrage(rates, nodes)

        if cycle is None:
            pytest.skip("No arbitrage cycle found — skipping profit test")

        profit = compute_cycle_profit(cycle, rates)
        assert profit > 0, f"BF returned cycle {cycle} but profit={profit:.6f}% is not positive"


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Graph Queries (DB required)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestGraphQueries:
    """Integration tests for Cypher query helpers in graph_queries.py."""

    def test_find_high_spread_edges(self, db_conn: psycopg.Connection):
        """find_high_spread_edges(threshold=0.0) must return valid edges."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        edges = find_high_spread_edges(db_conn, threshold=0.0)
        assert isinstance(edges, list)

        required_keys = {"src", "dst", "bid", "ask", "spread"}
        for edge in edges:
            assert required_keys.issubset(edge.keys())
            assert isinstance(edge["spread"], float)
            assert edge["spread"] >= 0

    def test_find_high_spread_edges_threshold(self, db_conn: psycopg.Connection):
        """Impossibly high threshold → empty list."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        edges = find_high_spread_edges(db_conn, threshold=9_999_999.0)
        assert edges == []

    def test_crypto_subgraph(self, db_conn: psycopg.Connection):
        """crypto_subgraph must return nodes with BTC/ETH and valid edges."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = crypto_subgraph(db_conn)
        assert isinstance(result, dict)
        assert "nodes" in result
        assert "edges" in result

        nodes = result["nodes"]
        known_cryptos = {"BTC", "ETH"}
        assert known_cryptos & set(nodes)

        node_set = set(nodes)
        for edge in result["edges"]:
            assert edge["src"] in node_set
            assert edge["dst"] in node_set

    def test_find_shortest_path_direct(self, db_conn: psycopg.Connection):
        """find_shortest_path('BTC', 'USD') → valid path."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = find_shortest_path(db_conn, "BTC", "USD")
        assert result is not None
        assert "path" in result
        path = result["path"]
        assert len(path) >= 2
        assert path[0] == "BTC"
        assert path[-1] == "USD"

    def test_find_shortest_path_no_path(self, db_conn: psycopg.Connection):
        """Non-existent path → None."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = find_shortest_path(db_conn, "FAKECOIN", "FAKEFIAT")
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: Graph API (HTTP, async, requires running graph-service)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestGraphAPI:
    """HTTP integration tests for the graph-service REST API."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, graph_client):
        response = await graph_client.get("/graph/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["node_count"] > 0

    @pytest.mark.asyncio
    async def test_nodes_endpoint(self, graph_client):
        response = await graph_client.get("/graph/nodes")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        for node in body:
            assert "symbol" in node
            assert "asset_type" in node

    @pytest.mark.asyncio
    async def test_edges_endpoint(self, graph_client):
        response = await graph_client.get("/graph/edges")
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        assert len(body) >= 1
        for edge in body:
            assert {"bid", "ask", "spread"}.issubset(edge.keys())

    @pytest.mark.asyncio
    async def test_rates_matrix_endpoint(self, graph_client):
        response = await graph_client.get("/graph/rates")
        assert response.status_code == 200
        body = response.json()
        assert "nodes" in body and "matrix" in body and "size" in body
        assert body["size"] == len(body["nodes"])

    @pytest.mark.asyncio
    async def test_signals_endpoint(self, graph_client):
        response = await graph_client.get("/graph/signals", params={"limit": 10})
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body, list)
        for signal in body:
            assert "path" in signal
            assert "profit_pct" in signal
            assert signal["method"] == "CLASSICAL"

    @pytest.mark.asyncio
    async def test_benchmark_endpoint(self, graph_client):
        response = await graph_client.get("/graph/benchmark", params={"n_nodes": 8, "n_trials": 5})
        assert response.status_code == 200
        body = response.json()
        assert body["n_nodes"] == 8
        assert isinstance(body["mean_ms"], (int, float))
        assert isinstance(body["min_ms"], (int, float))


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Graph Init
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
class TestGraphInit:
    """Tests for graph_init.init_graph — idempotency and data completeness."""

    def test_init_graph_idempotent(self, db_conn: psycopg.Connection):
        """Calling init_graph twice must return consistent results."""
        first = init_graph(db_conn)
        assert first["node_count"] >= 20
        assert first["edge_count"] >= 50

        second = init_graph(db_conn)
        assert second["node_count"] == first["node_count"]
        assert second["edge_count"] == first["edge_count"]

    def test_all_crypto_assets_present(self, db_conn: psycopg.Connection):
        """After init, all 10 expected crypto nodes must exist."""
        init_graph(db_conn)
        expected = {"BTC", "ETH", "LINK", "SOL", "ADA", "XRP", "DOGE", "AVAX", "UNI", "DOT"}

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset {asset_type: 'crypto'}) RETURN a.symbol "
                "$cypher$) AS (sym agtype);"
            )
            rows = cur.fetchall()

        found = {str(row[0]).strip('"') for row in rows}
        missing = expected - found
        assert not missing, f"Missing crypto assets: {missing}. Found: {found}"

    def test_all_fiat_assets_present(self, db_conn: psycopg.Connection):
        """After init, all 10 expected fiat nodes must exist."""
        init_graph(db_conn)
        expected = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "INR", "SGD", "HKD"}

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset {asset_type: 'fiat'}) RETURN a.symbol "
                "$cypher$) AS (sym agtype);"
            )
            rows = cur.fetchall()

        found = {str(row[0]).strip('"') for row in rows}
        missing = expected - found
        assert not missing, f"Missing fiat assets: {missing}. Found: {found}"

    def test_exchange_edges_have_valid_bids(self, db_conn: psycopg.Connection):
        """All EXCHANGE edges must have bid > 0 and ask > 0."""
        init_graph(db_conn)

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset)-[r:EXCHANGE]->(b:Asset) "
                "RETURN a.symbol, b.symbol, r.bid, r.ask "
                "$cypher$) AS (src agtype, dst agtype, bid agtype, ask agtype);"
            )
            rows = cur.fetchall()

        assert len(rows) > 0, "No EXCHANGE edges found."
        invalid = []
        for row in rows:
            src = str(row[0]).strip('"')
            dst = str(row[1]).strip('"')
            try:
                bid = float(str(row[2]))
                ask = float(str(row[3]))
            except (ValueError, TypeError):
                invalid.append(f"{src}→{dst}: unparseable bid/ask")
                continue
            if bid <= 0:
                invalid.append(f"{src}→{dst}: bid={bid}")
            if ask <= 0:
                invalid.append(f"{src}→{dst}: ask={ask}")

        assert not invalid, f"Edges with non-positive bid/ask: {invalid}"

    # ── New graph init tests ──────────────────────────────────────────────────

    def test_edge_count_matches_expected(self, db_conn: psycopg.Connection):
        """After init, verify graph has substantial edges (≥100 for 20 nodes)."""
        result = init_graph(db_conn)
        assert result["edge_count"] >= 100, (
            f"Expected ≥100 edges for a 20-node graph, got {result['edge_count']}"
        )

    def test_crypto_subgraph_has_10_nodes(self, db_conn: psycopg.Connection):
        """crypto_subgraph must return exactly 10 crypto nodes."""
        init_graph(db_conn)
        result = crypto_subgraph(db_conn)
        crypto_nodes = result["nodes"]
        assert len(crypto_nodes) == 10, (
            f"Expected 10 crypto nodes, got {len(crypto_nodes)}: {crypto_nodes}"
        )

    def test_fiat_nodes_have_correct_type(self, db_conn: psycopg.Connection):
        """All fiat nodes in the graph must have asset_type='fiat'."""
        init_graph(db_conn)

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset {asset_type: 'fiat'}) RETURN a.symbol, a.asset_type "
                "$cypher$) AS (sym agtype, atype agtype);"
            )
            rows = cur.fetchall()

        assert len(rows) >= 10, f"Expected ≥10 fiat nodes, got {len(rows)}"
        for row in rows:
            atype = str(row[1]).strip('"')
            assert atype == "fiat", f"Node {str(row[0]).strip(chr(34))} has type '{atype}', expected 'fiat'"
