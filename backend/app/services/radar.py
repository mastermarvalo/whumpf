"""Radar (NOAA MRMS base reflectivity, 1 km, every 2 min): shared pieces.

The job (app/jobs/radar_refresh.py, every 5 min) keeps one frame per 10
minutes for the last two hours. Each frame is the newest MRMS file in the
10 minutes up to its time, clipped to the region:

    dem-cogs/radar/<region>/<YYYYMMDDHHMM>-<sha8>.tif   one per frame, never rewritten
    dem-cogs/radar/<region>/latest.json                 the frames, oldest first

/tiles/radar/* (routers/weather_tiles.py) reads latest.json. Values are dBZ;
under 5 dBZ, MRMS's "no echo" (-99) and "no coverage" (-999) are all nodata
(-999), so the tiles are transparent there.
docs/runbook.md §9.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.services import rasterlayer

NODATA = -999.0
ATTRIBUTION = "NOAA/NWS MRMS"
ATTRIBUTION_URL = "https://www.nssl.noaa.gov/projects/mrms/"
FRAME = timedelta(minutes=10)
# Frames shown: the slider covers the last two hours (13 frames).
WINDOW = timedelta(hours=2)
# MRMS updates every 2 min and the newest frame is published 3 min after its
# time, so a healthy layer is at most ~15 min old.
STALE_AFTER = timedelta(minutes=20)
MAXZOOM = 9  # 0.01° (~1 km) grid; clients overzoom past this
MIN_DBZ = 5.0

# NWS-style reflectivity steps in dBZ. Snow is mostly under 30 dBZ.
SCALE: rasterlayer.Scale = [
    (5, 10, "#04e9e7"),
    (10, 15, "#019ff4"),
    (15, 20, "#0300f4"),
    (20, 25, "#02fd02"),
    (25, 30, "#01c501"),
    (30, 35, "#008e00"),
    (35, 40, "#fdf802"),
    (40, 45, "#e5bc00"),
    (45, 50, "#fd9500"),
    (50, 55, "#fd0000"),
    (55, 60, "#d40000"),
    (60, 65, "#bc0000"),
    (65, 1000, "#f800fd"),
]

TITILER_COLORMAP = rasterlayer.titiler_colormap(SCALE)


def legend() -> list[dict[str, Any]]:
    return rasterlayer.legend(SCALE)


def frame_id(t: datetime) -> str:
    return f"{t.astimezone(UTC):%Y%m%d%H%M}"


def prefix(region: str) -> str:
    return f"radar/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, frame: datetime, sha8: str) -> str:
    return f"{prefix(region)}/{frame_id(frame)}-{sha8}.tif"


_pointers: dict[str, tuple[dict | None, float]] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not run yet. Raises
    if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)
