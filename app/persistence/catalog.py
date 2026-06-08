"""Route-by-meaning: ``$vectorSearch`` over one collection.

This is the gateway's *only* selection mechanism. Each proxied GitHub tool (minus
the destructive ones the safety floor hides) becomes one document in
``tool_catalog`` with a **local** embedding of its name + description (Ollama
``nomic-embed-text``, 768-dim, no keys). A free-form ``X-MCP-Query`` is embedded
the same way and matched against the catalog with MongoDB ``$vectorSearch``; the
top-k tools come back. No tags, no hand-written policy, no keyword arm: relevance
is learned entirely from the tools' own descriptions, by meaning.

Everything fails open: no Mongo, index not ready, or no local embedder, and the
caller falls back to serving the full proxied catalog -- never worse than a plain
firehose proxy.
"""

from __future__ import annotations

import logging
from typing import Any

from pymongo.operations import SearchIndexModel

from app.ollama import embed
from app.persistence.client import get_database
from app.settings import Settings, settings

logger = logging.getLogger("mcpx.catalog")

CATALOG = "tool_catalog"
VECTOR_INDEX = "tool_vector"


def _embed_text(tool: Any) -> str:
    """The text the embedder sees: glossed name + description.

    Glossing the snake_case name into words ("pull_request_read" -> "pull request
    read") lets the embedding capture the tool's *purpose*, not just its literal
    identifier -- which sharpens semantic matches against plain-language queries.
    """

    name = getattr(tool, "name", "") or ""
    description = getattr(tool, "description", None) or ""
    glossed = name.replace("_", " ")
    return f"{name} {glossed}\n{description}".strip()


# ---------------------------------------------------------------------------
# Seeding + index
# ---------------------------------------------------------------------------

def _vector_index_model(cfg: Settings) -> SearchIndexModel:
    return SearchIndexModel(
        definition={
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": cfg.embed_dimensions,
                    "similarity": "cosine",
                }
            ]
        },
        name=VECTOR_INDEX,
        type="vectorSearch",
    )


async def _existing_search_indexes(coll) -> dict[str, dict[str, Any]]:
    try:
        cursor = await coll.list_search_indexes()
        return {ix["name"]: ix async for ix in cursor}
    except Exception:  # noqa: BLE001 -- not all deployments support search indexes
        return {}


async def _ensure_indexes(db, cfg: Settings) -> None:
    coll = db[CATALOG]
    existing = await _existing_search_indexes(coll)
    if VECTOR_INDEX in existing:
        return
    try:
        await coll.create_search_index(_vector_index_model(cfg))
        logger.info("created search index: %s", VECTOR_INDEX)
    except Exception as exc:  # noqa: BLE001
        logger.warning("search index create skipped (%s): %s", VECTOR_INDEX, exc)


async def ensure_seeded(cfg: Settings = settings) -> None:
    """Embed the proxied catalog into ``tool_catalog`` + build the vector index (idempotent).

    Best-effort and safe to call on startup: returns immediately when Mongo is
    off, and leaves the gateway serving the full catalog if the local embedder is
    missing. Reuses the demo catalog loader so there's one source of truth
    for "what tools exist" -- which means destructive tools hidden by the safety
    floor are never embedded, and therefore never retrievable.
    """

    db = get_database()
    if db is None:
        return
    try:
        coll = db[CATALOG]
        if await coll.count_documents({}) > 0:
            await _ensure_indexes(db, cfg)
            return

        # One source of truth: the same proxied, safety-floored catalog the live
        # gateway and demo routes use.
        from app.gateway.preview import _load_catalog

        tools = list(await _load_catalog())
        if not tools:
            return
        texts = [_embed_text(t) for t in tools]
        vectors = await embed(texts, model=cfg.embed_model, host=cfg.ollama_host)
        if vectors is None:
            # Vector search is the only arm -- with no embedder there is nothing to
            # seed. Leave the catalog empty so the gateway serves the full catalog.
            logger.warning("local embedder unavailable -- skipping catalog seed (gateway serves full catalog).")
            return

        docs: list[dict[str, Any]] = []
        for idx, tool in enumerate(tools):
            if idx >= len(vectors):
                break
            docs.append(
                {
                    "_id": tool.name,
                    "name": tool.name,
                    "description": tool.description or "",
                    "embedding": vectors[idx],
                }
            )

        await coll.delete_many({})
        await coll.insert_many(docs)
        logger.info("seeded %d tools into %s", len(docs), CATALOG)
        await _ensure_indexes(db, cfg)
    except Exception as exc:  # noqa: BLE001 -- seeding is pure upside, never fatal
        logger.warning("catalog seeding skipped: %s", exc)


# ---------------------------------------------------------------------------
# Retrieval ($vectorSearch)
# ---------------------------------------------------------------------------

async def _vector_arm(
    coll, query_vector: list[float], k: int, cfg: Settings
) -> list[dict[str, Any]]:
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX,
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": cfg.route_num_candidates,
                "limit": k,
            }
        },
        {"$project": {"_id": 0, "name": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    try:
        cursor = await coll.aggregate(pipeline)
        return [doc async for doc in cursor]
    except Exception as exc:  # noqa: BLE001 -- index may be building
        logger.debug("vector search skipped: %s", exc)
        return []


async def search_tools(
    query: str,
    *,
    k: int = 8,
    cfg: Settings = settings,
) -> list[str] | None:
    """Rank the catalog by meaning. Returns ranked tool names, or ``None`` to fall back.

    Embeds the query locally and runs ``$vectorSearch`` over the full
    (safety-floored) catalog, returning the top-k names. ``None`` signals the
    caller to serve the full catalog (Mongo off, no embedder, index not ready, or
    no match).
    """

    db = get_database()
    if db is None or not query:
        return None
    try:
        query_vectors = await embed(query, model=cfg.embed_model, host=cfg.ollama_host)
        if not query_vectors:
            return None
        hits = await _vector_arm(db[CATALOG], query_vectors[0], k, cfg)
        if not hits:
            return None
        return [h["name"] for h in hits]
    except Exception as exc:  # noqa: BLE001
        logger.debug("route-by-meaning skipped: %s", exc)
        return None


async def explain_route(
    query: str,
    *,
    k: int = 8,
    cfg: Settings = settings,
) -> dict[str, Any]:
    """Return the ranked ``$vectorSearch`` hits (with scores) for the UI inspector."""

    db = get_database()
    out: dict[str, Any] = {"hits": [], "selected": [], "embedder_available": False}
    if db is None or not query:
        return out

    query_vectors = await embed(query, model=cfg.embed_model, host=cfg.ollama_host)
    out["embedder_available"] = bool(query_vectors)
    if not query_vectors:
        return out

    hits = await _vector_arm(db[CATALOG], query_vectors[0], k, cfg)
    out["hits"] = hits
    out["selected"] = [h["name"] for h in hits]
    return out


async def route_status(cfg: Settings = settings) -> dict[str, Any]:
    """Readiness for the dashboard's route-by-meaning panel."""

    db = get_database()
    if db is None:
        return {
            "enabled": False,
            "reason": "MongoDB not configured (MCPX_MONGO_URI unset).",
            "catalog_size": 0,
        }
    try:
        size = await db[CATALOG].count_documents({})
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "reason": f"catalog unavailable: {exc}", "catalog_size": 0}
    if size == 0:
        return {
            "enabled": False,
            "reason": "Catalog not seeded yet -- embeddings + the vector index are still building.",
            "catalog_size": 0,
        }
    # Confirm the vector index exists and is queryable before promising routing.
    indexes = await _existing_search_indexes(db[CATALOG])
    ix = indexes.get(VECTOR_INDEX)
    if ix is None or not ix.get("queryable", False):
        return {
            "enabled": False,
            "reason": "Vector index is still building -- try again shortly.",
            "catalog_size": size,
        }
    return {"enabled": True, "reason": None, "catalog_size": size}
