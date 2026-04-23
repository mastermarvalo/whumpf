"""FastAPI application entrypoint."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config import get_settings
from app.routers import health, terrain, tiles

logger = logging.getLogger("whumpf")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Whumpf API",
        version=__version__,
        description="Backcountry terrain intelligence.",
        docs_url="/docs",
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers — more will be added as phases progress.
    app.include_router(health.router)
    app.include_router(tiles.router)
    app.include_router(terrain.router)

    logger.info("Whumpf API starting (env=%s, version=%s)", settings.whumpf_env, __version__)
    return app


app = create_app()
