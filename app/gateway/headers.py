"""Tiny helper for reading the gateway's one request header.

The gateway's entire interface is a single header: ``x-mcp-query`` -- a free-form
task description that drives semantic retrieval. Nothing to declare up front,
nothing to configure: just say what you're doing and the gateway hands back the
tools that match by meaning.
"""

from __future__ import annotations

from fastmcp.server.dependencies import get_http_headers

from app.settings import settings


def _current_headers() -> dict[str, str]:
    try:
        return get_http_headers()
    except Exception:  # noqa: BLE001 -- no request context (e.g. background seeding)
        return {}


def header_value(headers: dict[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup (FastMCP lowercases keys, but be safe)."""

    if not headers:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def query_value() -> str | None:
    value = header_value(_current_headers(), settings.query_header)
    value = value.strip() if value else None
    return value or None
