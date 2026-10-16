"""Wind now (NOAA RTMA, hourly 2.5 km analysis): shared pieces.

The hourly RTMA job (app/jobs/temperature_refresh.py) reads the 10 m wind
direction, speed and gust from the same analysis file as the temperature
(one extra Range request) and writes a point grid of arrows per region:

    dem-cogs/wind/<region>/<YYYYMMDDHH>-<sha8>.geojson.gz   one per revision
    dem-cogs/wind/<region>/latest.json                      what is served

The arrows are one gzipped GeoJSON FeatureCollection on a STEP_DEG grid.
Each point has `dir` (degrees true the wind blows FROM), `mph`, `gust_mph`
and `lvl`: 0 on every 4th row and column (0.2°), 1 on every 2nd (0.1°),
else 2. The app shows `lvl <= max_lvl` for the zoom band in LEVELS, so
arrows thin out when zoomed out and never move when zooming in.

/tiles/wind/* (routers/weather_tiles.py) serves meta and the files.
docs/runbook.md §9.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import timedelta

import httpx

from app.config import get_settings
from app.http_retry import call_with_resilience
from app.services import rasterlayer

ATTRIBUTION = "NOAA/NWS RTMA"
ATTRIBUTION_URL = "https://www.nco.ncep.noaa.gov/pmb/products/rtma/"
# Same hourly analysis as temperature.
STALE_AFTER = timedelta(hours=3)
STEP_DEG = 0.05
# MapLibre zoom bands (maxzoom exclusive) -> the highest lvl to show.
LEVELS = [
    {"minzoom": 0, "maxzoom": 8, "max_lvl": 0, "step_deg": 0.2},
    {"minzoom": 8, "maxzoom": 9, "max_lvl": 1, "step_deg": 0.1},
    {"minzoom": 9, "maxzoom": 24, "max_lvl": 2, "step_deg": 0.05},
]
REVISION = re.compile(r"^\d{10}-[0-9a-f]{8}$")


def prefix(region: str) -> str:
    return f"wind/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def arrows_key(region: str, revision: str) -> str:
    return f"{prefix(region)}/{revision}.geojson.gz"


_pointers: dict[str, tuple[dict | None, float]] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet. Raises
    if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)


# Arrow files never change once written, so keep the newest few per worker.
_arrows: OrderedDict[str, bytes] = OrderedDict()
_ARROWS_MAX = 32
_HTTP = httpx.AsyncClient(timeout=10.0)


async def read_arrows(region: str, revision: str) -> bytes | None:
    """The gzipped GeoJSON for one revision, or None if there is no such
    file (never written, or cleaned up). Raises if MinIO can't be read."""
    key = arrows_key(region, revision)
    if key in _arrows:
        _arrows.move_to_end(key)
        return _arrows[key]

    settings = get_settings()
    url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{key}"

    async def _do() -> httpx.Response:
        r = await _HTTP.get(url)
        # MinIO answers 403 for a missing key when listing isn't public.
        if r.status_code not in (403, 404):
            r.raise_for_status()
        return r

    r = await call_with_resilience("minio", _do)
    if r.status_code in (403, 404):
        return None
    _arrows[key] = r.content
    while len(_arrows) > _ARROWS_MAX:
        _arrows.popitem(last=False)
    return r.content
