"""
Rate Limiter Middleware — Redis sliding-window, 1000 req/s/IP.

Falls back to a threading.Semaphore token-bucket if Redis is unavailable.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

import redis.asyncio as aioredis
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("rate_limiter")

RATE_LIMIT = 1000       # max requests per window per IP
WINDOW_SECONDS = 1      # sliding window size

# Module-level Redis client (lazy init)
_redis_client: aioredis.Redis | None = None

# Fallback in-memory sliding window (for when Redis is down)
# Maps ip -> deque of timestamps
_local_windows: dict[str, deque] = defaultdict(deque)
_local_lock = threading.Lock()


def init_redis(url: str = "redis://redis:6379") -> None:
    """Initialise the Redis client. Call once at startup."""
    global _redis_client
    try:
        _redis_client = aioredis.from_url(url, socket_connect_timeout=1, socket_timeout=1)
        logger.info("Rate limiter: Redis client initialized at %s", url)
    except Exception as exc:
        logger.warning("Rate limiter: Could not init Redis (%s) — using in-memory fallback", exc)
        _redis_client = None


async def _redis_check(client_ip: str) -> bool:
    """
    Redis INCR + EXPIRE sliding window check.
    Returns True if request is allowed, False if rate-limited.
    """
    key = f"rl:{client_ip}"
    try:
        count = await _redis_client.incr(key)
        if count == 1:
            await _redis_client.expire(key, WINDOW_SECONDS)
        return count <= RATE_LIMIT
    except Exception as exc:
        logger.debug("Redis rate-limit check failed (%s), falling back to local", exc)
        return None  # Signal to use local fallback


def _local_check(client_ip: str) -> bool:
    """In-memory sliding window fallback."""
    now = time.monotonic()
    cutoff = now - WINDOW_SECONDS
    with _local_lock:
        window = _local_windows[client_ip]
        # Evict expired entries
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            return False
        window.append(now)
        return True


async def rate_limit_middleware(request: Request, call_next):
    """
    Check rate limit for incoming request.
    Returns 429 if over limit, otherwise passes through.
    """
    client_ip = request.client.host if request.client else "0.0.0.0"

    allowed = None
    if _redis_client is not None:
        allowed = await _redis_check(client_ip)

    if allowed is None:
        allowed = _local_check(client_ip)

    if not allowed:
        logger.warning("RATE_LIMIT exceeded: ip=%s path=%s", client_ip, request.url.path)
        return JSONResponse(
            {"error": "rate limit exceeded", "limit": RATE_LIMIT, "window_seconds": WINDOW_SECONDS},
            status_code=429,
        )

    return await call_next(request)
