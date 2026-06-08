"""Single-process FastAPI + smart-MCP gateway.

This module wires the moving parts:

* A plain FastAPI app for ``/health`` and the ``/demo`` token-savings dashboard.
* A FastMCP **proxy** (``Lean-MCP-Gateway``) forwarding to the official remote
  GitHub MCP server.
* A server-level safety-floor transform plus the per-request
  :class:`SemanticFilterMiddleware`, which retrieves tools by meaning from a
  free-form ``x-mcp-query`` via MongoDB ``$vectorSearch``.

The MCP server is exposed via FastMCP's Streamable HTTP transport
(``mcp.http_app``), mounted at ``/mcp``. The FastAPI app adopts the MCP ASGI
app's lifespan so the session manager initializes (nested lifespans are not
auto-detected).
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastmcp import FastMCP
from pydantic import BaseModel

from app.gateway.github_proxy import build_github_proxy
from app.gateway.middleware import SemanticFilterMiddleware
from app.gateway.preview import build_preview
from app.gateway.transforms import SafetyFloorTransform
from app.persistence import shutdown_mongo, startup_mongo
from app.settings import settings

STATIC_DIR = Path(__file__).parent / "static"

# Colloquial demo queries for the "route by meaning" inspector. Each is worded so
# it shares *no* keywords with GitHub's terse tool descriptions -- the match has
# to come from meaning, via the embedding, not from a literal keyword overlap.
ROUTE_EXAMPLES: list[dict[str, str]] = [
    {"label": "\u201cthe build's been red all morning\u201d", "query": "the build's been red all morning"},
    {"label": "\u201ccan someone look at my change?\u201d", "query": "can someone look at my change before it ships?"},
    {"label": "\u201cwhat did we ship last week?\u201d", "query": "what did we ship to users last week?"},
    {"label": "\u201ctrack down that crash report\u201d", "query": "track down the crash report a user filed"},
    {"label": "\u201cwho can push to this repo?\u201d", "query": "who has permission to push to this repository?"},
    {"label": "\u201cstamp a new version for users\u201d", "query": "stamp a new version and ship it to users"},
]


class TryRequest(BaseModel):
    """Payload for the live ``/demo/try`` raw-vs-smart comparison (fully local)."""

    model: str | None = None
    host: str | None = None
    task: str
    expected_tool: str | None = None


def build_mcp_server() -> FastMCP:
    """Construct the gateway: a proxy to the real GitHub MCP server."""

    mcp = build_github_proxy(settings)

    # Safety floor: destructive tools are NEVER exposed (the only static rule).
    mcp.add_transform(SafetyFloorTransform(enabled=settings.block_destructive))
    # Per-request semantic selection (x-mcp-query -> vector top-k + cache).
    # Descriptions are passed through untouched -- retrieval only changes *how
    # many* tools the model sees, never their text.
    mcp.add_middleware(SemanticFilterMiddleware())

    return mcp


def build_app() -> FastAPI:
    mcp = build_mcp_server()

    # path="/" because we mount the ASGI app under the "/mcp" prefix below.
    mcp_app = mcp.http_app(path="/")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await startup_mongo()
        # Seed the route-by-meaning catalog in the background if empty, so the
        # gateway's semantic retrieval works after `docker compose up` with no manual step.
        # Best-effort: failures (no Mongo, no embedder) leave the gateway serving
        # the full catalog -- never worse than a plain proxy.
        from app.persistence.catalog import ensure_seeded

        seed_task = asyncio.create_task(ensure_seeded())
        try:
            async with mcp_app.lifespan(app):
                yield
        finally:
            seed_task.cancel()
            await shutdown_mongo()

    api = FastAPI(
        title="Smart MCP Gateway (semantic tool retrieval)",
        lifespan=lifespan,
    )

    @api.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy", "environment": os.getenv("ENV", settings.env)}

    @api.get("/")
    @api.get("/demo")
    async def dashboard() -> FileResponse:
        """Serve the single-page token-savings dashboard."""

        return FileResponse(
            STATIC_DIR / "dashboard.html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @api.get("/demo/preview")
    async def demo_preview(q: str | None = Query(default=None)) -> dict:
        """Live raw-firehose vs smart-retrieval ``tools/list`` token comparison.

        With ``q`` the smart side is the semantic top-k for that task; without it
        the smart side is the full catalog. Descriptions are byte-for-byte
        identical on both sides -- only the tool count differs.
        """

        return await build_preview(q)

    @api.get("/demo/route/config")
    async def demo_route_config() -> dict:
        """Readiness + curated colloquial queries for the 'route by meaning' inspector."""

        from app.persistence.catalog import route_status

        status = await route_status()
        return {
            "enabled": bool(status.get("enabled")),
            "reason": status.get("reason"),
            "catalog_size": status.get("catalog_size"),
            "embed_model": settings.embed_model,
            "embed_dimensions": settings.embed_dimensions,
            "top_k": settings.route_top_k,
            "examples": ROUTE_EXAMPLES,
        }

    @api.get("/demo/route")
    async def demo_route(
        q: str = Query(..., min_length=1),
        k: int | None = Query(default=None),
    ) -> dict:
        """Explain how a free-form query routes to tools via MongoDB $vectorSearch."""

        from app.persistence.catalog import explain_route, route_status

        status = await route_status()
        resp: dict = {
            "enabled": bool(status.get("enabled")),
            "reason": status.get("reason"),
            "catalog_size": status.get("catalog_size"),
            "query": q,
        }
        if not status.get("enabled"):
            return resp
        detail = await explain_route(q, k=k or settings.route_top_k)
        resp.update(detail)
        return resp

    @api.get("/demo/try/config")
    async def demo_try_config() -> dict:
        """Static config for the live 'try it yourself' panel."""

        from app.gateway.playground import playground_config

        return await playground_config()

    @api.post("/demo/try")
    async def demo_try(req: TryRequest, request: Request) -> dict:
        """Run a task through a local model: raw catalog vs smart gateway."""

        from app.gateway.playground import PlaygroundError, run_playground

        url = str(request.base_url).rstrip("/") + "/mcp/"
        try:
            return await run_playground(
                url=url,
                task=req.task,
                model=req.model,
                host=req.host,
                expected_tool=req.expected_tool,
            )
        except PlaygroundError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/demo/try/stream")
    async def demo_try_stream(req: TryRequest, request: Request) -> StreamingResponse:
        """Stream the live run as Server-Sent Events so the UI can show what the
        local model is doing -- prefill, token output, per-pass timing.
        """

        from app.gateway.playground import run_playground_stream

        url = str(request.base_url).rstrip("/") + "/mcp/"

        async def events():
            try:
                async for ev in run_playground_stream(
                    url=url,
                    task=req.task,
                    model=req.model,
                    host=req.host,
                    expected_tool=req.expected_tool,
                ):
                    yield f"data: {json.dumps(ev)}\n\n"
            except Exception as exc:  # noqa: BLE001 -- last resort, surface to client
                yield f"data: {json.dumps({'type': 'error', 'detail': str(exc)})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    api.mount("/mcp", mcp_app)
    return api


combined_app = build_app()
