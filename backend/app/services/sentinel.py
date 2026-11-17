"""Recent Sentinel-2 imagery ("latest clear view"): shared pieces.

The daily job (app/jobs/sentinel_refresh.py) keeps, for every pixel of the
region, the newest Sentinel-2 L2A view that the scene classification (SCL)
calls clear, and publishes it as three COGs in MinIO:

    dem-cogs/sentinel/<region>/true/<revision>.tif    true colour, RGB JPEG + mask
    dem-cogs/sentinel/<region>/false/<revision>.tif   SWIR/NIR/red, RGB JPEG + mask
    dem-cogs/sentinel/<region>/date/<revision>.tif    capture day, uint16 (0 = none)
    dem-cogs/sentinel/<region>/dates/<revision>.json.gz  capture day per ~1 km cell
    dem-cogs/sentinel/<region>/latest.json            what is served

The dates grid is how the app shows "imagery from Nov 14" for the map
centre without sending a location to the server, offline too.

Pixels with no clear view in the last WINDOW_DAYS are empty (transparent).
/tiles/sentinel/* (routers/sentinel.py) serves them; access goes through
app/entitlements.py ("sentinel"). docs/runbook.md §19.

Data: Copernicus Sentinel-2 L2A as COGs on AWS Open Data
(s3://sentinel-cogs, no account), found through Element 84's Earth Search
STAC API. Free and open under the Copernicus terms; every use must say
"Contains modified Copernicus Sentinel data <year>".
"""

from __future__ import annotations

import base64
import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
from rasterio.transform import Affine, from_origin
from rasterio.warp import transform_bounds

from app.services import rasterlayer

EARTH_SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
COLLECTION = "sentinel-2-l2a"
# Only files from here are ever read.
HREF_PREFIX = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/"

PRODUCTS = ("true", "false")
WINDOW_DAYS = 30
# Web Mercator metres a pixel: ~9.7 m on the ground at 39°N, close to the
# 10 m bands. Tiles are served to MAXZOOM (z14 is ~7.4 m) and overzoomed after.
RES_M = 12.5
MAXZOOM = 14
STALE_AFTER = timedelta(hours=48)  # daily job: two missed runs

ATTRIBUTION_URL = "https://sentinels.copernicus.eu/documents/247904/690755/Sentinel_Data_Legal_Notice"

# SCL classes taken as a clear view: topographic/dark shadow (2, else north
# faces in winter would always be empty), vegetation (4), bare (5), water
# (6), unclassified (7), snow and ice (11). Not: no data (0), saturated (1),
# cloud shadow (3), cloud (8, 9), cirrus (10).
CLEAR_SCL = (2, 4, 5, 6, 7, 11)

# The stretch: reflectance 0..1 with gamma 2.2, so snow keeps its texture
# (ESA's TCI clips at 0.2 and turns every snowfield flat white). Bump RENDER
# when the pixels would change: the job then rebuilds from scratch.
GAMMA = 2.2
RENDER = "g22-v1"
# Earth Search's L2A COGs have the BOA offset applied: reflectance x 10000.
SCALE = 10000.0

EPOCH = date(2015, 1, 1)  # date pixels count days from here; 0 = no view


def day_number(d: date) -> int:
    return (d - EPOCH).days


def from_day_number(n: int) -> date:
    return EPOCH + timedelta(days=int(n))


def attribution(dates: list[str] | None, today: date | None = None) -> str:
    """Copernicus's required credit, with the year(s) of the data shown."""
    if dates:
        first, last = dates[0][:4], dates[-1][:4]
        years = first if first == last else f"{first}-{last}"
    else:
        years = str((today or datetime.now(UTC).date()).year)
    return f"Contains modified Copernicus Sentinel data {years}"


def stretch(reflectance: np.ndarray) -> np.ndarray:
    """uint16 reflectance x 10000 -> uint8, gamma 2.2 over 0..1."""
    r = np.clip(reflectance.astype(np.float32) / SCALE, 0.0, 1.0)
    return np.round(np.power(r, 1.0 / GAMMA) * 255.0).astype(np.uint8)


def render(bands: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """The two products from the bands (reflectance x 10000):
    true = red, green, blue; false = SWIR 1.6 µm, NIR (8A), red, where snow
    is cyan, cloud white, forest green and rock brown."""
    red = stretch(bands["red"])
    return {
        "true": np.stack([red, stretch(bands["green"]), stretch(bands["blue"])]),
        "false": np.stack([stretch(bands["swir16"]), stretch(bands["nir08"]), red]),
    }


class Grid:
    """The region's pixel grid: Web Mercator, RES_M, snapped to whole pixels."""

    crs = "EPSG:3857"

    def __init__(self, bbox: tuple[float, float, float, float], res: float = RES_M) -> None:
        west, south, east, north = transform_bounds("EPSG:4326", self.crs, *bbox)
        self.res = res
        self.x0, self.y1 = math.floor(west / res) * res, math.ceil(north / res) * res
        self.width = math.ceil((east - self.x0) / res)
        self.height = math.ceil((self.y1 - south) / res)
        self.transform: Affine = from_origin(self.x0, self.y1, res, res)

    def key(self) -> list[float]:
        return [self.x0, self.y1, self.res, self.width, self.height]

    def window_of(self, bbox_lonlat: tuple[float, float, float, float]) -> tuple[int, int, int, int] | None:
        """(col0, row0, col1, row1) of the grid under a lon/lat box, or None."""
        west, south, east, north = transform_bounds("EPSG:4326", self.crs, *bbox_lonlat)
        col0 = max(math.floor((west - self.x0) / self.res), 0)
        col1 = min(math.ceil((east - self.x0) / self.res), self.width)
        row0 = max(math.floor((self.y1 - north) / self.res), 0)
        row1 = min(math.ceil((self.y1 - south) / self.res), self.height)
        return (col0, row0, col1, row1) if col1 > col0 and row1 > row0 else None


# ── the dates grid ───────────────────────────────────────────────────────────

DATES_CELL_DEG = 0.01  # ~1.1 x 0.85 km at 40°N


def dates_doc(days: np.ndarray, west: float, north: float, cell: float = DATES_CELL_DEG) -> dict:
    """The dates grid as JSON: `dates` lists the capture days shown, and
    `cells` (base64, one byte a cell, rows north to south) holds 0 for no
    view or i for dates[i - 1]. The cell at lon, lat is
    row floor((north - lat) / cell), column floor((lon - west) / cell)."""
    present = sorted(int(d) for d in np.unique(days) if d)
    if len(present) > 255:
        raise ValueError("more than 255 capture days in one grid")
    index = np.zeros(65536, dtype=np.uint8)
    for i, day in enumerate(present, 1):
        index[day] = i
    cells = index[days.astype(np.uint16)]
    height, width = days.shape
    return {
        "west": round(west, 6), "north": round(north, 6), "cell_deg": cell,
        "width": width, "height": height,
        "dates": [from_day_number(d).isoformat() for d in present],
        "cells": base64.b64encode(cells.tobytes()).decode(),
    }


def date_at(doc: dict, lon: float, lat: float) -> str | None:
    """The capture day at lon, lat from a dates grid (what the app does)."""
    row = math.floor((doc["north"] - lat) / doc["cell_deg"])
    col = math.floor((lon - doc["west"]) / doc["cell_deg"])
    if not (0 <= row < doc["height"] and 0 <= col < doc["width"]):
        return None
    i = base64.b64decode(doc["cells"])[row * doc["width"] + col]
    return doc["dates"][i - 1] if i else None


# ── keys and latest.json ──────────────────────────────────────────────────────

def prefix(region: str) -> str:
    return f"sentinel/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def cog_key(region: str, product: str, revision: str) -> str:
    return f"{prefix(region)}/{product}/{revision}.tif"


def dates_key(region: str, revision: str) -> str:
    return f"{prefix(region)}/dates/{revision}.json.gz"


_pointers: dict[str, tuple[dict | None, float]] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if the job has not published yet.
    Raises if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)
