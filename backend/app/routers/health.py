"""Health and readiness endpoints.

Two levels:

* ``/healthz``   — process is alive. Used by container orchestration.
* ``/readyz``    — all downstream dependencies (DB, S3, etc.) are reachable.
                   Used by the frontend and by NPM health checks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness probe. Must never touch the database."""
    return {"status": "ok", "version": __version__}


@router.get("/readyz")
def readyz() -> dict[str, Any]:
    """Readiness probe. Checks each dependency and reports status per-service.

    Returns 200 even if dependencies are down; callers should read the
    ``ready`` flag rather than relying on HTTP status. This keeps the
    endpoint useful for debugging ("which thing is broken?") rather than
    just a binary up/down.
    """
    settings = get_settings()
    checks: dict[str, dict[str, Any]] = {}

    # --- Database ----------------------------------------------------------
    try:
        engine = get_engine()
        with engine.connect() as conn:
            version = conn.execute(text("SELECT PostGIS_Full_Version()")).scalar_one()
        checks["database"] = {"ok": True, "postgis": str(version).split()[1] if version else None}
    except Exception as exc:  # noqa: BLE001
        logger.warning("database readiness check failed: %s", exc)
        checks["database"] = {"ok": False, "error": str(exc)}

    ready = all(c["ok"] for c in checks.values())
    return {
        "ready": ready,
        "env": settings.whumpf_env,
        "version": __version__,
        "checks": checks,
    }
