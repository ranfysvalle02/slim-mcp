"""Async MongoDB lifecycle -- and the discipline that keeps Mongo *off* the hot path.

MongoDB is optional everywhere. When ``MCPX_MONGO_URI`` is unset (or the cluster
is unreachable at startup), :func:`get_database` returns ``None`` and every
writer/reader degrades to a no-op, so the gateway and the live demo run exactly
the same -- just without telemetry/audit/route-by-meaning. A telemetry backend
must never be able to take down the gateway.

Uses PyMongo's native async API (``AsyncMongoClient``), not the deprecated Motor
driver.
"""

from __future__ import annotations

import logging

from pymongo import AsyncMongoClient

from app.settings import Settings, settings

logger = logging.getLogger("mcpx.persistence")

_client: AsyncMongoClient | None = None
_db = None


async def startup_mongo(cfg: Settings = settings) -> None:
    """Connect to Mongo if configured. Failures are logged, never raised."""

    global _client, _db
    if not cfg.mongo_uri:
        logger.info("MCPX_MONGO_URI unset -- persistence disabled (no-op mode).")
        return
    try:
        client: AsyncMongoClient = AsyncMongoClient(
            cfg.mongo_uri, serverSelectionTimeoutMS=3000
        )
        await client.admin.command("ping")
    except Exception as exc:  # noqa: BLE001 -- a telemetry backend can't take down the gateway
        logger.warning("MongoDB unreachable at startup (%s) -- running without persistence.", exc)
        _client = None
        _db = None
        return
    _client = client
    _db = client[cfg.mongo_database]
    logger.info("MongoDB connected: database=%s", cfg.mongo_database)


def get_database():
    """Return the active database handle, or ``None`` in no-op mode."""

    return _db


async def shutdown_mongo() -> None:
    global _client, _db
    if _client is not None:
        try:
            await _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client = None
    _db = None
