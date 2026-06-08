"""The per-request brain: semantic tool selection, caching, and telemetry.

On ``on_list_tools`` it reads the caller's free-form ``x-mcp-query``. If present
(and the embedded catalog is ready), it retrieves the top-k tools by *meaning*
via MongoDB ``$vectorSearch`` and returns only those -- the
tools' descriptions are passed through byte-for-byte; the *only* thing that
changes versus the firehose is how many tools come back. With no query -- or if
retrieval can't run -- it serves the full (safety-floored) catalog, so the
gateway is never worse than an honest proxy. Either way it caches the result for
a short TTL and records the resulting token cost.

On ``on_call_tool`` it forwards to GitHub and records an audit row (query, tool,
redacted args, latency). The safety floor (a transform) already makes destructive
tools invisible to both list and lookup-by-name, so there's no separate
authorization step here.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import Tool, ToolResult

from app.gateway.headers import query_value
from app.settings import Settings, settings
from app.tokens import measure_tools


class _TTLCache:
    """A tiny bounded TTL + LRU-ish cache for resolved tool lists."""

    def __init__(self, *, maxsize: int, ttl: float):
        self.maxsize = maxsize
        self.ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any | None:
        item = self._store.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: Any, value: Any) -> None:
        if len(self._store) >= self.maxsize and key not in self._store:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            self._store.pop(oldest, None)
        self._store[key] = (time.monotonic() + self.ttl, value)


def _fingerprint(tools: Sequence[Tool]) -> int:
    """A cheap content fingerprint so the cache busts if the catalog changes."""

    return hash(tuple(sorted((t.name, len(t.description or "")) for t in tools)))


class SemanticFilterMiddleware(Middleware):
    """Query-driven, per-request tool selection by meaning."""

    def __init__(self, cfg: Settings = settings):
        self.cfg = cfg
        self._cache = _TTLCache(
            maxsize=cfg.list_cache_max_entries, ttl=cfg.list_cache_ttl_seconds
        )

    async def on_list_tools(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = list(await call_next(context))

        # Lazy import keeps persistence optional and avoids import cycles.
        from app.persistence.telemetry import record_token_telemetry

        query = query_value()

        key = ((query or "").lower(), _fingerprint(tools))
        selected = self._cache.get(key)
        if selected is None:
            selected = await self._select_tools(tools, query)
            self._cache.set(key, selected)

        list_tokens, _ = measure_tools(selected)
        await record_token_telemetry(
            query=query,
            tool_count=len(selected),
            list_tokens=list_tokens,
        )
        return selected

    async def _select_tools(
        self, tools: Sequence[Tool], query: str | None
    ) -> list[Tool]:
        """Retrieve the top-k tools for the query by meaning; else the full catalog."""

        full = list(tools)
        if not query or not self.cfg.semantic_retrieval_enabled or not full:
            return full

        from app.persistence.catalog import search_tools

        ranked_names = await search_tools(query, k=self.cfg.route_top_k, cfg=self.cfg)
        if not ranked_names:
            return full  # fail-open: no Mongo / no embedder / indexes not ready
        by_name = {t.name: t for t in full}
        picked = [by_name[name] for name in ranked_names if name in by_name]
        return picked or full

    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, ToolResult],
    ) -> ToolResult:
        from app.persistence.telemetry import record_invocation_audit

        query = query_value()
        tool_name = context.message.name

        t0 = time.perf_counter()
        result = await call_next(context)
        latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)

        await record_invocation_audit(
            query=query,
            tool=tool_name,
            arguments=context.message.arguments,
            latency_ms=latency_ms,
        )
        return result
