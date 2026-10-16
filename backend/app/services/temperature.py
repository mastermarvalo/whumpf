"""Current temperature (NOAA RTMA, hourly 2.5 km analysis): shared pieces.

The hourly job (app/jobs/temperature_refresh.py) takes the 2 m temperature
out of the newest RTMA analysis, reprojects it to lon/lat, clips it to each
region and stores it as a COG in MinIO:

    dem-cogs/temperature/<region>/<YYYYMMDDHH>-<sha8>.tif   one per revision
    dem-cogs/temperature/<region>/latest.json               what is served

/tiles/temperature/* (routers/weather_tiles.py) reads latest.json. Values are
°F. Nodata is -9999. docs/runbook.md §9.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.services import rasterlayer

NODATA = -9999.0
ATTRIBUTION = "NOAA/NWS RTMA"
ATTRIBUTION_URL = "https://www.nco.ncep.noaa.gov/pmb/products/rtma/"
# Hourly analysis that appears ~40 min after the hour; three missed hours
# means the cron stopped or NOAA stopped publishing.
STALE_AFTER = timedelta(hours=3)
MAXZOOM = 9  # 2.5 km grid; clients overzoom past this

# Stepped scale in °F with a hard change at freezing. Open at both ends.
SCALE: rasterlayer.Scale = [
    (-1000, -10, "#4a2a7a"),
    (-10, 0, "#6c4fb0"),
    (0, 10, "#4560c8"),
    (10, 20, "#3f8fd2"),
    (20, 32, "#7fc6e8"),
    (32, 40, "#b7e2b1"),
    (40, 50, "#e6f2a2"),
    (50, 60, "#fee08b"),
    (60, 70, "#fdae61"),
    (70, 80, "#f46d43"),
    (80, 1000, "#c2304a"),
]

TITILER_COLORMAP = rasterlayer.titiler_colormap(SCALE)


def legend() -> list[dict[str, Any]]:
    return rasterlayer.legend(SCALE)


def prefix(region: str) -> str:
    return f"temperature/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, revision: str) -> str:
    return f"{prefix(region)}/{revision}.tif"


_pointers: dict[str, tuple[dict | None, float]] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet. Raises
    if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)
