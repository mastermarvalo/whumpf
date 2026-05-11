"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app import __version__
from app.config import get_settings
from app.db import get_engine
from app.models import Base
from app.rate_limit import limiter
from app.routers import auth, avalanche, health, regions, snowpack, strava, terrain, tiles
from app.routers.avalanche import get_forecast, get_observations
from app.services.snotel import fetch_stations_geojson

logger = logging.getLogger("whumpf")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


async def _prewarm() -> None:
    results = await asyncio.gather(
        fetch_stations_geojson(),
        get_forecast(),
        get_observations(),
        return_exceptions=True,
    )
    names = ["SNOTEL", "CAIC forecast", "CAIC obs"]
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            logger.warning("Pre-warm %s failed: %s", name, result)
        else:
            logger.info("Pre-warm %s OK", name)


# Idempotent column adds for tables that pre-date their current model. Run
# on every startup since we use Base.metadata.create_all (no Alembic). Each
# statement is a no-op when the column / index already exists. Cheap.
_SCHEMA_PATCHES: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_token VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verification_token_expires_at TIMESTAMP WITH TIME ZONE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_reset_token_expires_at TIMESTAMP WITH TIME ZONE",
    "CREATE INDEX IF NOT EXISTS ix_users_email_verification_token ON users(email_verification_token)",
    "CREATE INDEX IF NOT EXISTS ix_users_password_reset_token ON users(password_reset_token)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS allowed_regions VARCHAR(255) NOT NULL DEFAULT 'colorado'",
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    engine = get_engine()
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _SCHEMA_PATCHES:
            conn.execute(text(stmt))
    logger.info("Database tables ensured.")
    asyncio.ensure_future(_prewarm())
    yield


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Whumpf API",
        version=__version__,
        description="Backcountry terrain intelligence.",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )

    # Explicit method/header lists — wildcards combined with allow_credentials=True
    # are an anti-pattern: they let any origin in the allow_origins set carry user
    # cookies/Authorization through any unfamiliar verb or header.
    # Explicit method/header lists — wildcards combined with allow_credentials=True
    # are an anti-pattern: they let any origin in the allow_origins set carry user
    # cookies/Authorization through any unfamiliar verb or header.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
        max_age=600,
    )

    # Rate limiter wiring — per-endpoint @limiter.limit("...") decorators in the
    # routers do the actual enforcement; this just exposes the request hook.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(strava.router)
    app.include_router(tiles.router)
    app.include_router(terrain.router)
    app.include_router(snowpack.router)
    app.include_router(avalanche.router)
    app.include_router(regions.router)

    logger.info("Whumpf API starting (env=%s, version=%s)", settings.whumpf_env, __version__)
    return app


app = create_app()
