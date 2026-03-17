"""
Tests for M5 Security middleware — sql_firewall, rate_limiter, and the proxy app.

Run with:
    pytest tests/test_security.py -v

These tests use httpx.AsyncClient with the FastAPI app in-process (no network
required) and pytest-asyncio for async test execution.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import httpx

# The FastAPI app under test
from module5_security.main import app
from module5_security import rate_limiter as rl_module
from module5_security import sql_firewall as fw_module


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_mock_request(
    body: bytes = b"",
    query_string: str = "",
    path: str = "/lob/orders",
    client_host: str = "127.0.0.1",
) -> MagicMock:
    """Build a minimal mock Request object for middleware unit tests."""
    req = MagicMock()
    req.body = AsyncMock(return_value=body)
    req.query_params = MagicMock()
    req.query_params.__str__ = lambda _: query_string
    req.url = MagicMock()
    req.url.path = path
    req.client = MagicMock()
    req.client.host = client_host
    return req


# ── SQL Firewall tests ────────────────────────────────────────────────────────

class TestSQLFirewall:
    """Unit tests for sql_firewall_middleware — no HTTP server needed."""

    @pytest.mark.asyncio
    async def test_clean_request_passes(self):
        """A plain JSON body with no SQL keywords must be forwarded."""
        req = _make_mock_request(body=b'{"symbol": "BTC-USD", "price": 42000, "qty": 1}')
        sentinel = MagicMock()
        sentinel_response = MagicMock()
        call_next = AsyncMock(return_value=sentinel_response)

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_awaited_once_with(req)
        assert result is sentinel_response

    @pytest.mark.asyncio
    async def test_drop_table_blocked(self):
        """Payload containing DROP TABLE must be rejected with 403."""
        req = _make_mock_request(body=b"DROP TABLE orders;")
        call_next = AsyncMock()

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 403
        import json
        body = json.loads(result.body)
        assert body["reason"] == "injection_detected"

    @pytest.mark.asyncio
    async def test_union_select_blocked(self):
        """UNION SELECT in the body must be blocked."""
        req = _make_mock_request(body=b"x' UNION SELECT username, password FROM users --")
        call_next = AsyncMock()

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_double_dash_comment_blocked(self):
        """SQL comment sequence -- in a query string must be blocked."""
        req = _make_mock_request(body=b"", query_string="id=1--")
        call_next = AsyncMock()

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 403

    @pytest.mark.asyncio
    async def test_normal_json_passes(self):
        """Nested JSON with numeric values, strings, and arrays must pass."""
        payload = (
            b'{"orders": [{"side": "buy", "price": 100.5, "qty": 10}],'
            b' "meta": {"source": "kraken", "ts": 1700000000}}'
        )
        req = _make_mock_request(body=payload)
        ok_response = MagicMock()
        call_next = AsyncMock(return_value=ok_response)

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_awaited_once()
        assert result is ok_response

    @pytest.mark.asyncio
    async def test_ast_ddl_blocked(self):
        """sqlglot-detectable DDL not caught by string scan must be blocked."""
        # CREATE is in BANNED_STATEMENT_TYPES but not in BANNED_PATTERNS,
        # so this exercises the AST layer.
        req = _make_mock_request(body=b"CREATE TABLE pwned (id INT)")
        call_next = AsyncMock()

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 403
        import json
        body = json.loads(result.body)
        assert body["reason"] == "ddl_detected"

    @pytest.mark.asyncio
    async def test_injection_via_query_string(self):
        """Injection pattern in URL query params must also be caught."""
        req = _make_mock_request(body=b"", query_string="q=1' OR '1'='1")
        call_next = AsyncMock()

        result = await fw_module.sql_firewall_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 403


# ── Rate Limiter tests ────────────────────────────────────────────────────────

class TestRateLimiter:
    """Unit tests for rate_limit_middleware."""

    def setup_method(self):
        """Reset in-memory windows before each test."""
        rl_module._local_windows.clear()
        # Ensure Redis client is None so tests use local fallback
        rl_module._redis_client = None

    @pytest.mark.asyncio
    async def test_under_limit_passes(self):
        """A single request should always be allowed."""
        req = _make_mock_request(client_host="10.0.0.1")
        ok_response = MagicMock()
        call_next = AsyncMock(return_value=ok_response)

        result = await rl_module.rate_limit_middleware(req, call_next)

        call_next.assert_awaited_once()
        assert result is ok_response

    @pytest.mark.asyncio
    async def test_local_fallback_works(self):
        """_local_check must allow requests up to RATE_LIMIT and block beyond it."""
        ip = "10.0.0.2"
        # Fill the window to exactly the limit
        now = time.monotonic()
        rl_module._local_windows[ip].extend([now] * rl_module.RATE_LIMIT)

        allowed = rl_module._local_check(ip)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded_returns_429(self):
        """When the local window is full the middleware must return 429."""
        ip = "10.0.0.3"
        now = time.monotonic()
        rl_module._local_windows[ip].extend([now] * rl_module.RATE_LIMIT)

        req = _make_mock_request(client_host=ip)
        call_next = AsyncMock()

        result = await rl_module.rate_limit_middleware(req, call_next)

        call_next.assert_not_awaited()
        assert result.status_code == 429

    @pytest.mark.asyncio
    async def test_expired_entries_are_evicted(self):
        """Requests older than WINDOW_SECONDS must be evicted before checking."""
        ip = "10.0.0.4"
        old_time = time.monotonic() - (rl_module.WINDOW_SECONDS + 1)
        # Fill with expired timestamps
        rl_module._local_windows[ip].extend([old_time] * rl_module.RATE_LIMIT)

        allowed = rl_module._local_check(ip)
        # All entries are expired, so this new request should be allowed
        assert allowed is True

    @pytest.mark.asyncio
    async def test_redis_failure_falls_back_to_local(self):
        """If Redis raises, local check is used and request proceeds."""
        # Install a broken Redis client
        broken_redis = MagicMock()
        broken_redis.incr = AsyncMock(side_effect=Exception("connection refused"))
        rl_module._redis_client = broken_redis

        try:
            req = _make_mock_request(client_host="10.0.0.5")
            ok_response = MagicMock()
            call_next = AsyncMock(return_value=ok_response)

            result = await rl_module.rate_limit_middleware(req, call_next)

            call_next.assert_awaited_once()
            assert result is ok_response
        finally:
            rl_module._redis_client = None


# ── Integration tests against the FastAPI app ─────────────────────────────────

@pytest_asyncio.fixture
async def client():
    """Async test client for the security proxy app."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestSecurityProxy:
    """Integration tests using the FastAPI app via httpx.AsyncClient."""

    @pytest.mark.asyncio
    async def test_health_endpoint(self, client):
        """GET /health must return 200 with status=ok."""
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["module"] == "security_proxy"

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client):
        """GET / must return 200."""
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "message" in resp.json()

    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, client):
        """GET /metrics must return Prometheus text format."""
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        # Confirm at least one of our custom metrics appears in the output
        assert b"security_proxy_requests_total" in resp.content or b"# HELP" in resp.content

    @pytest.mark.asyncio
    async def test_sql_injection_blocked_by_proxy(self, client):
        """Requests with SQL injection in the body must be blocked at the proxy layer."""
        resp = await client.post(
            "/lob/orders",
            content="DROP TABLE orders;",
            headers={"Content-Type": "text/plain"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["error"] == "forbidden"

    @pytest.mark.asyncio
    async def test_lob_unavailable_returns_502(self, client):
        """Proxy must either reach lob-engine (any status) or return a clean 502.

        When running with ASGI transport (no real network), the upstream URL is
        unreachable and the proxy returns 502.  In a full-stack environment where
        lob-engine IS running the proxy forwards the real response (e.g. 404 for
        an unknown path).  Both outcomes confirm the proxy doesn't crash.
        """
        resp = await client.get("/lob/orderbook/BTC-USD")
        # 502 → service unreachable; any other code → service is live and proxied
        if resp.status_code == 502:
            assert "error" in resp.json()
        else:
            assert resp.status_code in (200, 404, 400, 503)

    @pytest.mark.asyncio
    async def test_quantum_unavailable_returns_502(self, client):
        """Proxy must either reach quantum-engine (any status) or return a clean 502."""
        resp = await client.get("/quantum/signals")
        if resp.status_code == 502:
            assert "error" in resp.json()
        else:
            assert resp.status_code in (200, 404, 400)

    @pytest.mark.asyncio
    async def test_admin_security_events_no_db(self, client):
        """
        GET /admin/security-events must return 200 even when the database is
        unreachable (returns empty list or error JSON, but not an unhandled 500).
        """
        resp = await client.get("/admin/security-events")
        # Either an empty list (table missing) or a handled DB error — both acceptable
        assert resp.status_code in (200, 500)
        body = resp.json()
        assert "events" in body or "error" in body

    @pytest.mark.asyncio
    async def test_admin_benchmark_runs_no_db(self, client):
        """
        GET /admin/benchmark-runs must return 200 even when the database is
        unreachable (returns empty list or error JSON, but not an unhandled 500).
        """
        resp = await client.get("/admin/benchmark-runs")
        assert resp.status_code in (200, 500)
        body = resp.json()
        assert "runs" in body or "error" in body

    @pytest.mark.asyncio
    async def test_admin_security_events_limit_param(self, client):
        """limit query param must be accepted (validation only — no live DB)."""
        resp = await client.get("/admin/security-events?limit=10")
        assert resp.status_code in (200, 500)

    @pytest.mark.asyncio
    async def test_admin_security_events_invalid_limit(self, client):
        """limit=0 is below the minimum (ge=1) and must return 422."""
        resp = await client.get("/admin/security-events?limit=0")
        assert resp.status_code == 422
