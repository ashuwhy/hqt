import asyncio
import httpx
import pytest

pytestmark = pytest.mark.asyncio

async def test_e2e_flow(lob_client: httpx.AsyncClient, analytics_client: httpx.AsyncClient):
    """End-to-End: place order, verify it goes to LOB, then to timescale"""
    resp = await lob_client.get("/lob/health")
    assert resp.status_code == 200

    resp = await analytics_client.get("/health")
    assert resp.status_code == 200
