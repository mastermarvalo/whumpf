"""Recent snowfall (NOAA NOHRSC National Snowfall Analysis): shared pieces.

The hourly job (app/jobs/snowfall_refresh.py) clips NOHRSC's 24 h and 72 h
GeoTIFFs to each region and stores them as COGs in MinIO:

    dem-cogs/snowfall/<region>/<period>/<YYYYMMDDHH>-<sha8>.tif   one per revision
    dem-cogs/snowfall/<region>/latest.json                        what is served

Every revision gets a new key, so TiTiler never reads a half-written file and
tile URLs carry the revision (?v=) instead of needing a Cloudflare purge.
/tiles/snowfall/* (routers/tiles.py) reads latest.json to find the COG.
The machinery is shared with the weather layers: app/services/rasterlayer.py.

Values are inches of new snow (the NOHRSC GeoTIFFs are in inches; their
NetCDF twins are in metres). Nodata is -99999. docs/runbook.md §8.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from app.services import rasterlayer

logger = logging.getLogger("whumpf.snowfall")

PERIODS: dict[str, timedelta] = {"24h": timedelta(hours=24), "72h": timedelta(hours=72)}
NODATA = -99999.0
ATTRIBUTION = "NOAA/NWS NOHRSC National Snowfall Analysis"
ATTRIBUTION_URL = "https://www.nohrsc.noaa.gov/snowfall/"

# Stepped scale in inches: (min, max, hex). Below the first step is
# transparent. Served to the apps as the legend and to TiTiler as an interval
# colormap, so the two cannot drift apart.
SCALE: rasterlayer.Scale = [
    (0.1, 1, "#d6e9f8"),
    (1, 2, "#a6cfee"),
    (2, 4, "#6aaee0"),
    (4, 6, "#3a86c8"),
    (6, 8, "#1f5aa8"),
    (8, 12, "#4b3a9e"),
    (12, 18, "#7b3fa6"),
    (18, 24, "#a8389a"),
    (24, 36, "#cf2f7c"),
    (36, 1000, "#8c0f45"),
]

TITILER_COLORMAP = rasterlayer.titiler_colormap(SCALE)


def legend() -> list[dict[str, Any]]:
    return rasterlayer.legend(SCALE)


def prefix(region: str) -> str:
    return f"snowfall/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, period: str, revision: str) -> str:
    return f"{prefix(region)}/{period}/{revision}.tif"


# ── latest.json, read by the API ──────────────────────────────────────────────

_pointers: dict[str, tuple[dict | None, float]] = {}
_HTTP = rasterlayer._HTTP


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet.

    Raises (httpx / CircuitOpenError) if MinIO can't be read; callers answer
    503 no-store.
    """
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)
