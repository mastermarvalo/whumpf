"""Recent snowfall (NOAA NOHRSC National Snowfall Analysis): shared pieces.

The hourly job (app/jobs/snowfall_refresh.py) clips NOHRSC's 24 h and 72 h
GeoTIFFs to each region and stores them as COGs in MinIO:

    dem-cogs/snowfall/<region>/<period>/<YYYYMMDDHH>-<sha8>.tif   one per revision
    dem-cogs/snowfall/<region>/latest.json                        what is served

Every revision gets a new key, so TiTiler never reads a half-written file and
tile URLs carry the revision (?v=) instead of needing a Cloudflare purge.
/tiles/snowfall/* (routers/tiles.py) reads latest.json to find the COG.

Values are inches of new snow (the NOHRSC GeoTIFFs are in inches; their
NetCDF twins are in metres). Nodata is -99999. docs/runbook.md §8.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.http_retry import call_with_resilience

logger = logging.getLogger("whumpf.snowfall")

PERIODS: dict[str, timedelta] = {"24h": timedelta(hours=24), "72h": timedelta(hours=72)}
NODATA = -99999.0
ATTRIBUTION = "NOAA/NWS NOHRSC National Snowfall Analysis"
ATTRIBUTION_URL = "https://www.nohrsc.noaa.gov/snowfall/"

# Stepped scale in inches: (min, max, hex). Below the first step is
# transparent. Served to the apps as the legend and to TiTiler as an interval
# colormap, so the two cannot drift apart.
SCALE: list[tuple[float, float, str]] = [
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


def _rgba(hex_color: str) -> list[int]:
    h = hex_color.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)] + [255]


# TiTiler interval colormap: [[[min, max], [r, g, b, a]], ...], min inclusive.
TITILER_COLORMAP = json.dumps(
    [[[lo, hi], _rgba(color)] for lo, hi, color in SCALE], separators=(",", ":")
)


def legend() -> list[dict[str, Any]]:
    out = []
    for lo, hi, color in SCALE:
        label = f"{lo:g}+" if hi >= 1000 else f"{lo:g}–{hi:g}"
        out.append({"min": lo, "max": None if hi >= 1000 else hi, "color": color, "label": label})
    return out


def prefix(region: str) -> str:
    return f"snowfall/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, period: str, revision: str) -> str:
    return f"{prefix(region)}/{period}/{revision}.tif"


# ── latest.json, read by the API ──────────────────────────────────────────────
# Read over plain HTTP from the public dem-cogs bucket (as TiTiler does), and
# kept per worker for a minute: tiles hit this on every request.

_POINTER_TTL_S = 60.0
_pointers: dict[str, tuple[dict | None, float]] = {}

_HTTP = httpx.AsyncClient(timeout=5.0)


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet.

    Raises (httpx / CircuitOpenError) if MinIO can't be read; callers answer
    503 no-store.
    """
    cached = _pointers.get(region)
    if cached and time.monotonic() - cached[1] < _POINTER_TTL_S:
        return cached[0]

    settings = get_settings()
    url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{pointer_key(region)}"

    async def _do() -> httpx.Response:
        r = await _HTTP.get(url)
        # MinIO answers 403 for a missing key when listing isn't public.
        if r.status_code not in (403, 404):
            r.raise_for_status()
        return r

    r = await call_with_resilience("minio", _do)
    pointer = None if r.status_code in (403, 404) else r.json()
    _pointers[region] = (pointer, time.monotonic())
    return pointer
