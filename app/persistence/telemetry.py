"""Telemetry + audit writers -- analytics on live operational data, no warehouse.

Every ``tools/list`` records its post-retrieval token cost (and the query that
drove it); every ``tools/call`` records the query, tool, redacted args, and
latency. Arguments are redacted before storage. Both writers are fire-and-forget
and no-op when Mongo is off -- they can never fail a request.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.persistence.client import get_database

logger = logging.getLogger("mcpx.telemetry")

_SENSITIVE = ("password", "token", "secret", "authorization", "auth", "pat", "apikey", "api_key")
_MAX_VALUE_LEN = 200


def _now() -> datetime:
    return datetime.now(UTC)


def redact_args(args: Any) -> dict[str, Any]:
    """Mask secret-looking keys and truncate long values before storage."""

    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if any(token in str(key).lower() for token in _SENSITIVE):
            out[key] = "***"
        elif isinstance(value, str) and len(value) > _MAX_VALUE_LEN:
            out[key] = value[:_MAX_VALUE_LEN] + "\u2026"
        else:
            out[key] = value
    return out


async def record_token_telemetry(
    *,
    query: str | None,
    tool_count: int,
    list_tokens: int,
) -> None:
    db = get_database()
    if db is None:
        return
    try:
        await db["token_telemetry"].insert_one(
            {
                "query": query,
                "tool_count": tool_count,
                "list_tokens": list_tokens,
                "routed_by_query": bool(query),
                "created_at": _now(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("token telemetry write skipped: %s", exc)


async def record_invocation_audit(
    *,
    query: str | None,
    tool: str,
    arguments: Any,
    latency_ms: float,
) -> None:
    db = get_database()
    if db is None:
        return
    try:
        await db["invocation_audit"].insert_one(
            {
                "query": query,
                "tool": tool,
                "args": redact_args(arguments or {}),
                "latency_ms": latency_ms,
                "created_at": _now(),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("invocation audit write skipped: %s", exc)
