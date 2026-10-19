"""FastAPI application entrypoint."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app import __version__
from app.config import get_settings
from app.rate_limit import limiter
from app.routers import (
    auth,
    avalanche,
    cameras,
    friends,
    health,
    regions,
    roads,
    routes,
    snowpack,
    strava,
    terrain,
    tile_tokens,
    tiles,
    trips,
    weather,
    weather_tiles,
)
from app.routers import status as status_router
from app.routers.avalanche import get_forecast, get_observations, get_zones
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
        get_zones(),
        return_exceptions=True,
    )
    names = ["SNOTEL", "CAIC forecast", "CAIC obs", "CAIC zones"]
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            logger.warning("Pre-warm %s failed: %s", name, result)
        else:
            logger.info("Pre-warm %s OK", name)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    # Schema is managed by Alembic; entrypoint.sh runs `alembic upgrade head`
    # before uvicorn starts (once, not per worker). /readyz reports drift.
    asyncio.ensure_future(_prewarm())
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    # Interactive docs and the OpenAPI schema are dev-only; redoc is never served.
    docs_enabled = settings.whumpf_env != "prod"

    app = FastAPI(
        title="Whumpf API",
        version=__version__,
        description="Backcountry terrain intelligence.",
        docs_url="/docs" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
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
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
    app.include_router(tile_tokens.router)
    app.include_router(tiles.router)
    app.include_router(weather_tiles.router)
    app.include_router(weather.router)
    app.include_router(cameras.router)
    app.include_router(roads.router)
    app.include_router(roads.tiles_router)
    app.include_router(terrain.router)
    app.include_router(snowpack.router)
    app.include_router(avalanche.router)
    app.include_router(regions.router)
    app.include_router(routes.router)
    app.include_router(friends.router)
    app.include_router(trips.router)
    app.include_router(status_router.router)

    logger.info("Whumpf API starting (env=%s, version=%s)", settings.whumpf_env, __version__)
    return app


app = create_app()
