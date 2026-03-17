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

class TestBellmanFordIntegration:
    """Integration tests for build_rate_matrix and bellman_ford_arbitrage.

    All tests skip gracefully if the AGE graph has not been initialised yet
    (e.g. when running CI without a seeded database).
    """

    def test_build_rate_matrix_returns_data(self, db_conn: psycopg.Connection):
        """build_rate_matrix must return a non-empty rate dict and ≥2 nodes,
        where every rate is a positive float."""
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
            assert src in node_set, (
                f"src '{src}' from rates key not found in nodes list"
            )
            assert dst in node_set, (
                f"dst '{dst}' from rates key not found in nodes list"
            )

    def test_run_detector_inserts_signal(self, db_conn: psycopg.Connection):
        """If Bellman-Ford finds a profitable cycle, _insert_signal must
        write a CLASSICAL row to the arbitrage_signals table."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)
        if len(nodes) < 4:
            pytest.skip(f"Not enough nodes ({len(nodes)}) for a meaningful cycle check")

        cycle = bellman_ford_arbitrage(rates, nodes)
        if cycle is None:
            pytest.skip("No arbitrage cycle found in current graph state — skipping insertion test")

        profit_pct = compute_cycle_profit(cycle, rates)
        if profit_pct <= 0:
            pytest.skip("Detected cycle is not profitable — skipping insertion test")

        # Insert and immediately verify
        _insert_signal(db_conn, cycle, profit_pct, 1.23, len(nodes))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM arbitrage_signals WHERE method = 'CLASSICAL';"
            )
            row = cur.fetchone()

        assert row is not None, "Query for arbitrage_signals returned no row"
        count = int(row[0])
        assert count >= 1, (
            f"Expected at least 1 CLASSICAL row in arbitrage_signals, got {count}"
        )

    def test_compute_cycle_profit_positive(self, db_conn: psycopg.Connection):
        """If Bellman-Ford detects a cycle from live DB data, the computed
        profit percentage must be > 0."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        rates, nodes = build_rate_matrix(db_conn)
        cycle = bellman_ford_arbitrage(rates, nodes)

        if cycle is None:
            pytest.skip("No arbitrage cycle found in current graph state — skipping profit test")

        profit = compute_cycle_profit(cycle, rates)
        assert profit > 0, (
            f"BF returned cycle {cycle} but profit={profit:.6f}% is not positive"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Graph Queries (DB required)
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphQueries:
    """Integration tests for Cypher query helpers in graph_queries.py."""

    def test_find_high_spread_edges(self, db_conn: psycopg.Connection):
        """find_high_spread_edges(threshold=0.0) must return a list where every
        item has the keys src, dst, bid, ask, spread, and all spreads are ≥ 0."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        edges = find_high_spread_edges(db_conn, threshold=0.0)

        assert isinstance(edges, list), "Result must be a list"

        required_keys = {"src", "dst", "bid", "ask", "spread"}
        for edge in edges:
            assert required_keys.issubset(edge.keys()), (
                f"Edge missing required keys. Got: {set(edge.keys())}"
            )
            assert isinstance(edge["spread"], float), (
                f"spread must be a float, got {type(edge['spread'])}"
            )
            assert edge["spread"] >= 0, (
                f"spread must be non-negative, got {edge['spread']} "
                f"for edge {edge['src']} → {edge['dst']}"
            )

    def test_find_high_spread_edges_threshold(self, db_conn: psycopg.Connection):
        """find_high_spread_edges with an impossibly high threshold must return
        an empty list — no real edge can have a spread of 9,999,999."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        edges = find_high_spread_edges(db_conn, threshold=9_999_999.0)

        assert edges == [], (
            f"Expected empty list for threshold=9999999.0, got {len(edges)} edges"
        )

    def test_crypto_subgraph(self, db_conn: psycopg.Connection):
        """crypto_subgraph must return a dict with 'nodes' and 'edges'; nodes
        must include 'BTC' or 'ETH'; every edge src and dst must be in nodes."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = crypto_subgraph(db_conn)

        assert isinstance(result, dict), "crypto_subgraph must return a dict"
        assert "nodes" in result, "Result must have 'nodes' key"
        assert "edges" in result, "Result must have 'edges' key"

        nodes = result["nodes"]
        assert isinstance(nodes, list), "'nodes' must be a list"

        known_cryptos = {"BTC", "ETH"}
        assert known_cryptos & set(nodes), (
            f"Expected at least one of {known_cryptos} in nodes, got: {nodes}"
        )

        node_set = set(nodes)
        for edge in result["edges"]:
            assert "src" in edge and "dst" in edge, (
                f"Edge missing src/dst keys: {edge}"
            )
            assert edge["src"] in node_set, (
                f"Edge src '{edge['src']}' not in nodes list"
            )
            assert edge["dst"] in node_set, (
                f"Edge dst '{edge['dst']}' not in nodes list"
            )

    def test_find_shortest_path_direct(self, db_conn: psycopg.Connection):
        """find_shortest_path('BTC', 'USD') must return a non-None result with
        a 'path' key whose first element is 'BTC' and last is 'USD'."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = find_shortest_path(db_conn, "BTC", "USD")

        assert result is not None, (
            "Expected a path from BTC to USD, got None. "
            "Verify the graph has a BTC→USD EXCHANGE edge."
        )
        assert "path" in result, f"Result must have 'path' key, got: {result}"

        path = result["path"]
        assert isinstance(path, list), f"'path' must be a list, got {type(path)}"
        assert len(path) >= 2, f"Path must have ≥2 elements, got: {path}"
        assert path[0] == "BTC", f"Path must start with 'BTC', got: {path[0]}"
        assert path[-1] == "USD", f"Path must end with 'USD', got: {path[-1]}"

    def test_find_shortest_path_no_path(self, db_conn: psycopg.Connection):
        """find_shortest_path for two non-existent symbols must return None."""
        if not _graph_is_initialised(db_conn):
            pytest.skip("AGE graph not initialised — skipping DB test")

        result = find_shortest_path(db_conn, "FAKECOIN", "FAKEFIAT")

        assert result is None, (
            f"Expected None for non-existent path FAKECOIN→FAKEFIAT, got: {result}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: Graph API (HTTP, async, requires running graph-service)
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphAPI:
    """HTTP integration tests for the graph-service REST API.

    Requires a running graph-service instance reachable at the URL configured
    in the GRAPH_TEST_URL environment variable (default: http://graph-service:8003).
    All tests are marked @pytest.mark.integration and @pytest.mark.asyncio.
    """

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_endpoint(self, graph_client):
        """GET /graph/health must return 200 with status='ok' and node_count > 0."""
        response = await graph_client.get("/graph/health")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert "status" in body, f"Response missing 'status' key: {body}"
        assert body["status"] == "ok", (
            f"Expected status='ok', got status='{body['status']}'. Full body: {body}"
        )
        assert "node_count" in body, f"Response missing 'node_count' key: {body}"
        assert body["node_count"] > 0, (
            f"Expected node_count > 0, got {body['node_count']}"
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_nodes_endpoint(self, graph_client):
        """GET /graph/nodes must return 200 with a list; each item has
        'symbol' and 'asset_type'."""
        response = await graph_client.get("/graph/nodes")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert isinstance(body, list), f"Expected list response, got: {type(body)}"

        for node in body:
            assert "symbol" in node, f"Node missing 'symbol': {node}"
            assert "asset_type" in node, f"Node missing 'asset_type': {node}"
            assert isinstance(node["symbol"], str), (
                f"'symbol' must be a string, got {type(node['symbol'])}"
            )
            assert isinstance(node["asset_type"], str), (
                f"'asset_type' must be a string, got {type(node['asset_type'])}"
            )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_edges_endpoint(self, graph_client):
        """GET /graph/edges must return 200 with a list of ≥1 items; each item
        must have bid, ask, and spread fields."""
        response = await graph_client.get("/graph/edges")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert isinstance(body, list), f"Expected list response, got: {type(body)}"
        assert len(body) >= 1, (
            f"Expected ≥1 edges, got {len(body)}. Verify graph has been initialised."
        )

        required_keys = {"bid", "ask", "spread"}
        for edge in body:
            assert required_keys.issubset(edge.keys()), (
                f"Edge missing required keys. Expected {required_keys}, got {set(edge.keys())}"
            )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_rates_matrix_endpoint(self, graph_client):
        """GET /graph/rates must return 200 with 'nodes', 'matrix', and 'size'
        keys; size must equal len(nodes)."""
        response = await graph_client.get("/graph/rates")

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert "nodes" in body, f"Response missing 'nodes' key: {body}"
        assert "matrix" in body, f"Response missing 'matrix' key: {body}"
        assert "size" in body, f"Response missing 'size' key: {body}"

        nodes = body["nodes"]
        size = body["size"]
        assert isinstance(nodes, list), f"'nodes' must be a list, got {type(nodes)}"
        assert size == len(nodes), (
            f"'size' ({size}) must equal len(nodes) ({len(nodes)})"
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_signals_endpoint(self, graph_client):
        """GET /graph/signals?limit=10 must return 200 with a list; if non-empty,
        each item must have 'path', 'profit_pct', and method == 'CLASSICAL'."""
        response = await graph_client.get("/graph/signals", params={"limit": 10})

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert isinstance(body, list), f"Expected list response, got: {type(body)}"

        for signal in body:
            assert "path" in signal, f"Signal missing 'path': {signal}"
            assert "profit_pct" in signal, f"Signal missing 'profit_pct': {signal}"
            assert "method" in signal, f"Signal missing 'method': {signal}"
            assert signal["method"] == "CLASSICAL", (
                f"Expected method='CLASSICAL', got '{signal['method']}'"
            )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_benchmark_endpoint(self, graph_client):
        """GET /graph/benchmark?n_nodes=8&n_trials=5 must return 200 with
        'mean_ms', 'min_ms', and 'n_nodes' == 8."""
        response = await graph_client.get(
            "/graph/benchmark",
            params={"n_nodes": 8, "n_trials": 5},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}. Body: {response.text}"
        )

        body = response.json()
        assert "mean_ms" in body, f"Response missing 'mean_ms': {body}"
        assert "min_ms" in body, f"Response missing 'min_ms': {body}"
        assert "n_nodes" in body, f"Response missing 'n_nodes': {body}"
        assert body["n_nodes"] == 8, (
            f"Expected n_nodes=8, got {body['n_nodes']}"
        )
        assert isinstance(body["mean_ms"], (int, float)), (
            f"'mean_ms' must be numeric, got {type(body['mean_ms'])}"
        )
        assert isinstance(body["min_ms"], (int, float)), (
            f"'min_ms' must be numeric, got {type(body['min_ms'])}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Graph Init
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphInit:
    """Tests for graph_init.init_graph — idempotency and data completeness."""

    def test_init_graph_idempotent(self, db_conn: psycopg.Connection):
        """Calling init_graph twice must return consistent results.

        Both calls must return dicts with 'node_count' and 'edge_count';
        node_count must be ≥20 and edge_count must be ≥50;
        the second call must yield the same node and edge counts as the first
        (MERGE is idempotent — no duplicates should be created).
        """
        first = init_graph(db_conn)

        assert isinstance(first, dict), "init_graph must return a dict"
        assert "node_count" in first, f"Result missing 'node_count': {first}"
        assert "edge_count" in first, f"Result missing 'edge_count': {first}"
        assert first["node_count"] >= 20, (
            f"Expected ≥20 nodes after init, got {first['node_count']}"
        )
        assert first["edge_count"] >= 50, (
            f"Expected ≥50 edges after init, got {first['edge_count']}"
        )

        second = init_graph(db_conn)

        assert isinstance(second, dict), "Second init_graph call must return a dict"
        assert "node_count" in second, f"Second result missing 'node_count': {second}"
        assert "edge_count" in second, f"Second result missing 'edge_count': {second}"
        assert second["node_count"] == first["node_count"], (
            f"node_count changed between calls: {first['node_count']} → {second['node_count']}. "
            "MERGE should be idempotent."
        )
        assert second["edge_count"] == first["edge_count"], (
            f"edge_count changed between calls: {first['edge_count']} → {second['edge_count']}. "
            "MERGE should be idempotent."
        )

    def test_all_crypto_assets_present(self, db_conn: psycopg.Connection):
        """After init, all 10 expected crypto Asset nodes must be in the AGE graph."""
        init_graph(db_conn)

        expected_cryptos = {
            "BTC", "ETH", "LINK", "SOL", "ADA",
            "XRP", "DOGE", "AVAX", "UNI", "DOT",
        }

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset {asset_type: 'crypto'}) RETURN a.symbol "
                "$cypher$) AS (sym agtype);"
            )
            rows = cur.fetchall()

        found = {str(row[0]).strip('"') for row in rows}
        missing = expected_cryptos - found
        assert not missing, (
            f"The following crypto assets are missing from the graph: {missing}. "
            f"Found: {found}"
        )

    def test_all_fiat_assets_present(self, db_conn: psycopg.Connection):
        """After init, all 10 expected fiat Asset nodes must be in the AGE graph."""
        init_graph(db_conn)

        expected_fiats = {
            "USD", "EUR", "GBP", "JPY", "AUD",
            "CAD", "CHF", "INR", "SGD", "HKD",
        }

        with db_conn.cursor() as cur:
            cur.execute("SET search_path = ag_catalog, \"$user\", public;")
            cur.execute(
                "SELECT * FROM ag_catalog.cypher('fx_graph', $cypher$ "
                "MATCH (a:Asset {asset_type: 'fiat'}) RETURN a.symbol "
                "$cypher$) AS (sym agtype);"
            )
            rows = cur.fetchall()

        found = {str(row[0]).strip('"') for row in rows}
        missing = expected_fiats - found
        assert not missing, (
            f"The following fiat assets are missing from the graph: {missing}. "
            f"Found: {found}"
        )

    def test_exchange_edges_have_valid_bids(self, db_conn: psycopg.Connection):
        """All EXCHANGE edges must have bid > 0 and ask > bid (positive spread)."""
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

        assert len(rows) > 0, (
            "No EXCHANGE edges found. Verify init_graph ran successfully."
        )

        invalid_bid = []
        invalid_spread = []
        for row in rows:
            src = str(row[0]).strip('"')
            dst = str(row[1]).strip('"')
            try:
                bid = float(str(row[2]))
                ask = float(str(row[3]))
            except (ValueError, TypeError):
                invalid_bid.append(f"{src}→{dst}: unparseable bid/ask")
                continue

            if bid <= 0:
                invalid_bid.append(f"{src}→{dst}: bid={bid}")
            if ask <= bid:
                invalid_spread.append(f"{src}→{dst}: bid={bid} ask={ask}")

        assert not invalid_bid, (
            f"Edges with bid ≤ 0: {invalid_bid}"
        )
        assert not invalid_spread, (
            f"Edges where ask ≤ bid (non-positive spread): {invalid_spread}"
        )
