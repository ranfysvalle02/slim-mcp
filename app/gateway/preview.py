"""Live, read-only catalog loaders + safety-floor introspection.

Two cached loaders proxy the same remote GitHub MCP server the live gateway uses:

* :func:`_load_catalog` -- the gateway's **safety-floored** catalog (destructive
  tools dropped), kept byte-for-byte as published. This is also the source of
  truth for what gets embedded into the route-by-meaning catalog.
* :func:`_load_raw_catalog` -- the **literal** upstream catalog (every tool,
  including the destructive ones the floor hides).

:func:`safety_floor_status` diffs the two so the dashboard can show exactly what
the blocklist hides and why. Everything here is read-only -- nothing mutates
state, and the floor's behavior is unchanged.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp.tools import Tool

from app.settings import Settings, settings

_catalog_cache: list[Tool] | None = None
_catalog_lock = asyncio.Lock()

_raw_catalog_cache: list[Any] | None = None
_raw_catalog_lock = asyncio.Lock()


async def _load_catalog() -> list[Tool]:
    """Build the gateway's safety-floored tool catalog once and cache it (smart side).

    Proxies the same remote GitHub MCP server as the live gateway and applies the
    safety floor (destructive tools dropped), keeping full descriptions exactly as
    published. This is also the source of truth for what gets embedded into the
    route-by-meaning catalog.
    """

    global _catalog_cache
    if _catalog_cache is not None:
        return _catalog_cache
    async with _catalog_lock:
        if _catalog_cache is not None:
            return _catalog_cache
        from app.gateway.github_proxy import build_github_proxy, is_destructive

        mcp = build_github_proxy(settings, name="catalog-preview")
        tools = list(await mcp.list_tools(run_middleware=False))
        if settings.block_destructive:
            tools = [t for t in tools if not is_destructive(t)]
        _catalog_cache = tools
        return _catalog_cache


async def _load_raw_catalog() -> list[Any]:
    """List the *literal* upstream catalog -- the official GitHub MCP server.

    The real flat-injection baseline: every tool, full descriptions, NO safety
    floor. Authenticated server-side. Falls back to the gateway's catalog if the
    upstream is unreachable.
    """

    global _raw_catalog_cache
    if _raw_catalog_cache is not None:
        return _raw_catalog_cache
    async with _raw_catalog_lock:
        if _raw_catalog_cache is not None:
            return _raw_catalog_cache
        if settings.github_pat:
            from fastmcp import Client
            from fastmcp.client.transports import StreamableHttpTransport

            headers = {"Authorization": f"Bearer {settings.github_pat}"}
            if settings.github_readonly:
                headers["X-MCP-Readonly"] = "true"
            try:
                transport = StreamableHttpTransport(url=settings.github_remote_url, headers=headers)
                async with Client(transport=transport) as client:
                    _raw_catalog_cache = list(await client.list_tools())
                    return _raw_catalog_cache
            except Exception:  # noqa: BLE001 -- fall back to the gateway's catalog
                pass
        _raw_catalog_cache = list(await _load_catalog())
        return _raw_catalog_cache


async def safety_floor_status(cfg: Settings = settings) -> dict[str, Any]:
    """Read-only summary of the safety floor for the dashboard.

    Diffs the literal upstream catalog (destructive tools included) against the
    safety-floored catalog the gateway actually serves, so the UI can show
    exactly which destructive tools the blocklist hides -- and what drives the
    decision. Nothing here mutates state or changes what the floor blocks.
    """

    from app.gateway.github_proxy import DESTRUCTIVE_NEEDLES, is_destructive

    raw_catalog = await _load_raw_catalog()
    floored = await _load_catalog()
    blocked_names = sorted(
        getattr(t, "name", "") for t in raw_catalog if is_destructive(t)
    )
    return {
        "enabled": cfg.block_destructive,
        "needles": list(DESTRUCTIVE_NEEDLES),
        "blocked_count": len(blocked_names),
        "blocked_names": blocked_names,
        "raw_count": len(raw_catalog),
        "floored_count": len(floored),
    }
