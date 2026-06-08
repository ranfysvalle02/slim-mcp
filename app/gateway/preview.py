"""Live, read-only preview of the gateway's retrieval token math.

Powers the ``/demo`` dashboard's discovery meter as a literal A/B against two
*different servers*:

* **raw** -- the official remote GitHub MCP server, fetched live: every tool
  (including the destructive ones the gateway's safety floor hides), full
  descriptions. The true "flat injection" baseline a client pays with no gateway.
* **smart** -- this gateway, modelled with the *same* primitives it runs so the
  numbers match what an MCP client actually pays: the proxied catalog, the safety
  floor, and -- when a task query is given -- semantic retrieval of the top-k
  relevant tools via MongoDB ``$vectorSearch``.

The point of the demo is that the smart side changes exactly **one** variable:
how *many* tools come back. Descriptions are passed through byte-for-byte, so the
savings are 100% attributable to retrieval, not to trimming text. With no query
the smart side is the full (safety-floored) catalog; with a query it's only the
retrieved tools (the big win). Nothing here mutates state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastmcp.tools import Tool

from app.settings import Settings, settings
from app.tokens import measure_tools

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


def _tool_view(tool: Any, *, score: float | None = None) -> dict[str, Any]:
    description = getattr(tool, "description", None) or ""
    view: dict[str, Any] = {"name": getattr(tool, "name", ""), "description": description}
    if score is not None:
        view["score"] = round(score, 4)
    return view


def _side(tools: list[Any], *, mode: str) -> dict[str, Any]:
    """Measure a tool list exactly as published -- no rewriting, full descriptions."""

    tokens, payload_bytes = measure_tools(tools)
    return {
        "mode": mode,
        "tool_count": len(tools),
        "list_tokens": tokens,
        "payload_bytes": payload_bytes,
        "tools": [_tool_view(t) for t in tools],
    }


async def build_preview(query: str | None = None, cfg: Settings = settings) -> dict[str, Any]:
    """Return the raw-firehose vs smart-retrieval comparison for an optional task.

    Both sides carry full, byte-for-byte descriptions; the only difference is the
    number of tools -- so the savings are purely the work of semantic retrieval.
    """

    catalog = await _load_catalog()
    raw_catalog = await _load_raw_catalog()
    query = (query or "").strip() or None

    raw = _side(raw_catalog, mode="firehose")

    routed = False
    smart_tools: list[Tool] = catalog
    if query and cfg.semantic_retrieval_enabled:
        from app.persistence.catalog import search_tools

        ranked = await search_tools(query, k=cfg.route_top_k, cfg=cfg)
        if ranked:
            by_name = {t.name: t for t in catalog}
            picked = [by_name[name] for name in ranked if name in by_name]
            if picked:
                smart_tools = picked
                routed = True

    smart = _side(smart_tools, mode="semantic-top-k" if routed else "full-catalog")

    saved = raw["list_tokens"] - smart["list_tokens"]
    factor = (raw["list_tokens"] / smart["list_tokens"]) if smart["list_tokens"] else None

    return {
        "query": query,
        "routed": routed,
        "routing_ready": routed or not query,
        "route_top_k": cfg.route_top_k,
        "catalog_size": len(catalog),
        "raw": raw,
        "smart": smart,
        "savings": {
            "tokens_saved": saved,
            "factor": round(factor, 2) if factor else None,
        },
    }
