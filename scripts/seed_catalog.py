"""Seed the ``tool_catalog`` for route-by-meaning, then prove it's queryable.

This is the offline/deploy-time step the routing docs describe: it builds the
same catalog the live gateway proxies (the real GitHub MCP tools, minus the
destructive ones the safety floor hides), embeds each tool **locally** via Ollama,
writes one document per tool, and creates the MongoDB Vector Search index over
that single collection.

Prerequisites:

* MongoDB Atlas Local running (``docker compose up -d mongo``) and ``MCPX_MONGO_URI`` set.
* A local embedding model pulled: ``ollama pull nomic-embed-text``.

Run::

    MCPX_MONGO_URI="mongodb://localhost:27019/?directConnection=true" \\
        PYTHONPATH=. python scripts/seed_catalog.py

It is idempotent: re-running reuses an already-seeded catalog.
------------------------------------------------------------------------------------------------
------------------------------------------------------------------------------------------------
What does this script do?
------------------------------------------------------------------------------------------------
- It builds the knowledge base: To do semantic routing, the gateway needs to understand what every tool does. This script fetches the full list of tools from the GitHub MCP server.
- It creates the embeddings: It runs each tool's name and description through a local AI embedding model (via Ollama) to convert their text into vector embeddings.
- It populates the database: It saves these embeddings into MongoDB and creates a Vector Search index over them.
------------------------------------------------------------------------------------------------
The gateway acts as a "proxy" (a middleman).
When your LLM decides to actually run one of those tools, the gateway forwards that exact command straight to the official GitHub MCP server to execute it, and then passes the real result back to the LLM.
The database is only used as a search engine to figure out which of the real GitHub tools to show the LLM. The tools themselves, and the execution of those tools, are 100% official GitHub.
"""

from __future__ import annotations

import asyncio
import sys

from app.persistence import shutdown_mongo, startup_mongo
from app.persistence.catalog import ensure_seeded, explain_route, route_status
from app.persistence.client import get_database


async def _demo_query(query: str) -> None:
    detail = await explain_route(query, k=5)
    print(f'\n  q="{query}"')
    hits = detail.get("hits") or []
    if not hits:
        print("    (no hits)")
        return
    for h in hits:
        print(f"    {h['name']:<28} {h.get('score', 0.0):.3f}")


async def main() -> int:
    await startup_mongo()
    if get_database() is None:
        print("Mongo not configured. Set MCPX_MONGO_URI (e.g. `docker compose up -d mongo`).", file=sys.stderr)
        return 1

    await ensure_seeded()
    status = await route_status()
    print(f"route_status: {status}")
    if not status.get("enabled"):
        print(f"Routing not ready yet: {status.get('reason')}", file=sys.stderr)
        await shutdown_mongo()
        return 2

    # Prove retrieval by meaning: none of these share keywords with the tool names.
    await _demo_query("why did continuous integration fail")
    await _demo_query("leave a review on a teammate's pull request")
    await _demo_query("stamp a new version and ship it to users")

    await shutdown_mongo()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
