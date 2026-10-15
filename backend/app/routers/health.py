"""Health and readiness endpoints.

Two levels:

* ``/healthz``   — process is alive. Used by container orchestration.
* ``/readyz``    — all downstream dependencies (DB, S3, etc.) are reachable.
                   Used by the frontend and by NPM health checks.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db import get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

_ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@lru_cache
def _migration_heads() -> frozenset[str]:
    """Head revision(s) shipped with this build (read from migrations/)."""
    return frozenset(ScriptDirectory.from_config(Config(str(_ALEMBIC_INI))).get_heads())


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

    # --- Schema ------------------------------------------------------------
    # Not ready if the DB isn't at the migration head this build ships with
    # (forgot `make migrate`, or the DB is ahead of the code after a rollback).
    try:
        with get_engine().connect() as conn:
            current = frozenset(MigrationContext.configure(conn).get_current_heads())
        heads = _migration_heads()
        checks["schema"] = {"ok": current == heads, "revision": sorted(current), "head": sorted(heads)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("schema readiness check failed: %s", exc)
        checks["schema"] = {"ok": False, "error": str(exc)}

    ready = all(c["ok"] for c in checks.values())
    return {
        "ready": ready,
        "env": settings.whumpf_env,
        "version": __version__,
        "checks": checks,
    }
