"""Snow depth (NOAA NOHRSC SNODAS, daily 1 km): shared pieces.

The hourly job (app/jobs/snowdepth_refresh.py) takes the snow depth grid out
of the daily SNODAS archive that NSIDC publishes (data set G02158), clips it
to each region and stores it as a COG in MinIO:

    dem-cogs/snowdepth/<region>/<YYYYMMDD>-<sha8>.tif   one per revision
    dem-cogs/snowdepth/<region>/latest.json             what is served

/tiles/snowdepth/* (routers/weather_tiles.py) reads latest.json. Values are
inches (SNODAS stores millimetres). Nodata is -9999. docs/runbook.md §9.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.services import rasterlayer

NODATA = -9999.0
ATTRIBUTION = "NOAA/NWS NOHRSC SNODAS"
ATTRIBUTION_URL = "https://nsidc.org/data/g02158/versions/1"
# SNODAS is valid at 06Z each day and posts around midday; two missed days
# means the cron stopped or NOHRSC/NSIDC stopped publishing.
STALE_AFTER = timedelta(hours=48)
MAXZOOM = 10  # 1/120° (~1 km) grid; clients overzoom past this

# Stepped scale in inches. Under half an inch is transparent.
SCALE: rasterlayer.Scale = [
    (0.5, 6, "#dcebf7"),
    (6, 12, "#b3d3ee"),
    (12, 24, "#84b6e1"),
    (24, 36, "#5596d1"),
    (36, 48, "#3474bc"),
    (48, 72, "#2a52a0"),
    (72, 96, "#40388c"),
    (96, 120, "#5f2783"),
    (120, 1000, "#7d1670"),
]

TITILER_COLORMAP = rasterlayer.titiler_colormap(SCALE)


def legend() -> list[dict[str, Any]]:
    return rasterlayer.legend(SCALE)


def prefix(region: str) -> str:
    return f"snowdepth/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, revision: str) -> str:
    return f"{prefix(region)}/{revision}.tif"


_pointers: dict[str, tuple[dict | None, float]] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet. Raises
    if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)
