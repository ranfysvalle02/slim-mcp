"""Optional MongoDB persistence: lifecycle, telemetry/audit, and the
route-by-meaning catalog. All writers no-op when Mongo is off."""

from app.persistence.client import get_database, shutdown_mongo, startup_mongo

__all__ = ["get_database", "shutdown_mongo", "startup_mongo"]
