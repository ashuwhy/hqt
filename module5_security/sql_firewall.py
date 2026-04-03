"""
SQL Injection Firewall Middleware for HQT Security Proxy.

Two-layer detection:
  1. String scan - fast regex/string check for common injection patterns
  2. AST scan - sqlglot parses the payload and rejects DDL statements
"""
from __future__ import annotations

import logging
import os

import asyncpg
import sqlglot
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("sql_firewall")

_PG_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER','hqt')}"
    f":{os.getenv('POSTGRES_PASSWORD','hqt_secret')}"
    f"@{os.getenv('POSTGRES_HOST','postgres')}"
    f":5432/{os.getenv('POSTGRES_DB','hqt')}"
)

# Patterns that are always forbidden in request payloads
BANNED_PATTERNS = [
    "DROP ",
    "TRUNCATE ",
    "UNION SELECT",
    "--",
    "/*",
    "xp_",
    "EXEC(",
    "EXEC ",
    "information_schema",
    "'; ",
    "';",
    "\" OR ",
    "' OR ",
    "1=1",
    "1 = 1",
]

# sqlglot AST node types that indicate DDL/dangerous statements
BANNED_STATEMENT_TYPES = {"Drop", "TruncateTable", "Create", "AlterTable", "Command"}


async def _log_security_event(request: Request, event_type: str, payload_snippet: str) -> None:
    """Log a security event to console and security_events table."""
    client_ip = request.client.host if request.client else "unknown"
    logger.warning(
        "SECURITY EVENT: type=%s ip=%s path=%s payload=%s",
        event_type, client_ip, request.url.path, payload_snippet[:200],
    )
    try:
        conn = await asyncpg.connect(_PG_DSN)
        await conn.execute(
            """INSERT INTO security_events (client_ip, event_type, raw_payload, blocked, endpoint)
               VALUES ($1, $2, $3, TRUE, $4)""",
            client_ip, "SQL_INJECTION", payload_snippet[:500], request.url.path,
        )
        await conn.close()
    except Exception as e:
        logger.error("Failed to log security event to DB: %s", e)


async def sql_firewall_middleware(request: Request, call_next):
    """
    Inspect request body + query string for SQL injection patterns.

    Returns 403 JSONResponse if injection detected, otherwise passes through.
    """
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        body_str = ""

    query_str = str(request.query_params)
    combined = (body_str + " " + query_str).upper()

    # Layer 1: string scan
    for pattern in BANNED_PATTERNS:
        if pattern.upper() in combined:
            await _log_security_event(request, "SQL_INJECTION_STRING", body_str[:500])
            return JSONResponse({"error": "forbidden", "reason": "injection_detected"}, status_code=403)

    # Layer 2: AST scan (catches obfuscated payloads)
    try:
        for stmt in sqlglot.parse(body_str):
            if stmt is not None and type(stmt).__name__ in BANNED_STATEMENT_TYPES:
                await _log_security_event(request, "SQL_INJECTION_AST", body_str[:500])
                return JSONResponse({"error": "forbidden", "reason": "ddl_detected"}, status_code=403)
    except Exception:
        pass  # sqlglot parse errors are not injection attempts

    return await call_next(request)
