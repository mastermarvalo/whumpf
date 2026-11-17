"""NAIP aerial imagery at zooms 17-18 (docs/runbook.md §18): shared pieces.

USGS's own NAIP services stop at Colorado 2019, before the 2020 fires, so the
source is USDA's NAIP as cloud-optimized GeoTIFFs on Microsoft's Planetary
Computer (Colorado 2023 at 0.3 m). Public domain (USDA Farm Service Agency).

- `make naip-index` (app/jobs/naip_index.py, monthly cron) lists the region's
  NAIP files from Planetary Computer's STAC API and keeps the newest one of
  every quarter quad:

      dem-cogs/naip/<region>/<revision>.json   the file list, one per revision
      dem-cogs/naip/<region>/latest.json       what is served

- A tile is made by reading the files under it (newest first) at the
  overview that matches the zoom, warping to Web Mercator and encoding a
  JPEG. pack_cache does that once per tile and keeps it in
  naip_<region>.sqlite, for the live /tiles/naip route and for packs alike.
  A tile with no imagery under it is a real empty answer.
- The index revision is the source version, so a new index (a new NAIP
  year) remakes the tiles.
"""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds
from rasterio.windows import Window
from rasterio.windows import from_bounds as window_from_bounds
from shapely import STRtree, box

from app.config import get_settings
from app.http_retry import call_with_resilience
from app.services import rasterlayer, terrain_tiles

logger = logging.getLogger("whumpf.naip")

STAC_SEARCH_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
COLLECTION = "naip"
# Only files from here are ever read: the index can't point GDAL anywhere else.
HREF_PREFIX = "https://naipeuwest.blob.core.windows.net/naip/"

MINZOOM = 17
MAXZOOM = 18
TILE_PX = 256
JPEG_QUALITY = 80

ATTRIBUTION = "USDA Farm Service Agency: NAIP imagery"
ATTRIBUTION_URL = "https://naip-usdaonline.hub.arcgis.com/"
# The index is rebuilt monthly; two missed runs means the cron stopped.
STALE_AFTER = timedelta(days=45)

# Pixels read beyond the tile on each side, so bilinear resampling at the
# edge has its neighbours (no seams between tiles).
_BUF = 2

# vsicurl to Azure Blob Storage: no directory listing, no HEAD, and a failed
# read raises (it must never look like "no imagery here").
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_USE_HEAD": "FALSE",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_TIMEOUT": "20",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "VSI_CACHE": "TRUE",
}

# m_3910510_se_13_030_20230821_20240104: quad 3910510, quarter se.
_QUARTER_QUAD = re.compile(r"m_(\d{7})_(ne|nw|se|sw)_")


def user_agent() -> str:
    contact = get_settings().usgs_contact
    return f"whumpf/0.1 (+https://whumpf.co; {contact})" if contact else "whumpf/0.1 (+https://whumpf.co)"


def prefix(region: str) -> str:
    return f"naip/{region}"


def pointer_key(region: str) -> str:
    return f"{prefix(region)}/latest.json"


def index_key(region: str, revision: str) -> str:
    return f"{prefix(region)}/{revision}.json"


def quarter_quad(item_id: str) -> str | None:
    """'3910510_se' from a NAIP item id or file name; None if it has none."""
    m = _QUARTER_QUAD.search(item_id)
    return f"{m.group(1)}_{m.group(2)}" if m else None


@dataclass(frozen=True)
class Item:
    id: str
    href: str
    bbox: tuple[float, float, float, float]  # west, south, east, north (degrees)
    date: str                                # YYYY-MM-DD
    gsd: float


class NaipIndex:
    """One revision of a region's file list, searchable by box."""

    def __init__(self, key: str, revision: str, items: list[Item]) -> None:
        self.key = key
        self.revision = revision
        # Newest first, so a tile where two files overlap shows the newer.
        self.items = sorted(items, key=lambda i: (i.date, i.id), reverse=True)
        self._tree = STRtree([box(*i.bbox) for i in self.items])

    def covering(self, bounds: tuple[float, float, float, float]) -> list[Item]:
        """The files that touch bounds (west, south, east, north), newest first."""
        hits = sorted(int(i) for i in self._tree.query(box(*bounds)))
        return [self.items[i] for i in hits]


def items_from_json(doc: dict) -> list[Item]:
    items = []
    for raw in doc.get("items", []):
        if not str(raw.get("href", "")).startswith(HREF_PREFIX):
            raise ValueError(f"NAIP index has a file outside {HREF_PREFIX}")
        items.append(Item(id=raw["id"], href=raw["href"], bbox=tuple(raw["bbox"]),
                          date=raw["date"], gsd=float(raw["gsd"])))
    return items


# ── the index, read by the API and the pack worker ────────────────────────────

_pointers: dict[str, tuple[dict | None, float]] = {}
_indexes: dict[str, NaipIndex] = {}


async def read_pointer(region: str) -> dict | None:
    """latest.json for `region`, or None if make naip-index has not run.
    Raises if MinIO can't be read."""
    return await rasterlayer.read_pointer(pointer_key(region), _pointers)


async def load_index(region: str) -> NaipIndex | None:
    """The served index (kept per process until latest.json changes), or None
    if there is none yet. Raises if MinIO can't be read."""
    entry = ((await read_pointer(region)) or {}).get("current")
    if not entry:
        return None
    cached = _indexes.get(region)
    if cached is not None and cached.key == entry["key"]:
        return cached
    settings = get_settings()
    url = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{entry['key']}"

    async def _do():
        r = await rasterlayer._HTTP.get(url, timeout=30.0)
        r.raise_for_status()
        return r

    r = await call_with_resilience("minio", _do)
    index = NaipIndex(entry["key"], entry["revision"], items_from_json(r.json()))
    _indexes[region] = index
    return index


# ── a tile ───────────────────────────────────────────────────────────────────

def tile_lonlat_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = terrain_tiles.tile_mercator_bounds(z, x, y)
    return transform_bounds(terrain_tiles.WM_CRS, "EPSG:4326", xmin, ymin, xmax, ymax)


def _read_item(href: str, bounds: tuple[float, float, float, float]) -> tuple[np.ndarray, np.ndarray] | None:
    """One file's pixels under a tile, warped to the tile's 256 × 256 Web
    Mercator grid: (rgb uint8 (3, px, px), filled bool (px, px)), or None if
    the file doesn't reach the tile."""
    url = f"/vsicurl/{href}" if href.startswith("https://") else href  # a local file in tests
    total = TILE_PX + 2 * _BUF
    pw = (bounds[2] - bounds[0]) / TILE_PX
    ph = (bounds[3] - bounds[1]) / TILE_PX
    buffered = (bounds[0] - _BUF * pw, bounds[1] - _BUF * ph, bounds[2] + _BUF * pw, bounds[3] + _BUF * ph)

    with rasterio.open(url) as src:
        level = terrain_tiles._overview_for(src, buffered, total)
    opts = {} if level is None else {"overview_level": level}
    with rasterio.open(url, **opts) as src:
        left, bottom, right, top = transform_bounds(terrain_tiles.WM_CRS, src.crs, *buffered)
        raw = window_from_bounds(left, bottom, right, top, src.transform)
        col0, row0 = max(math.floor(raw.col_off), 0), max(math.floor(raw.row_off), 0)
        col1 = min(math.ceil(raw.col_off + raw.width), src.width)
        row1 = min(math.ceil(raw.row_off + raw.height), src.height)
        if col1 <= col0 or row1 <= row0:
            return None
        win = Window(col0, row0, col1 - col0, row1 - row0)
        data = src.read([1, 2, 3], window=win)
        src_transform = src.window_transform(win)
        src_crs = src.crs

    dst_transform = from_bounds(*buffered, total, total)
    rgb = np.zeros((3, total, total), dtype=np.uint8)
    reproject(source=data, destination=rgb, src_transform=src_transform, src_crs=src_crs,
              dst_transform=dst_transform, dst_crs=terrain_tiles.WM_CRS, resampling=Resampling.bilinear)
    # Where the file reaches: warp a mask of ones the same way.
    filled = np.zeros((total, total), dtype=np.uint8)
    reproject(source=np.ones(data.shape[1:], dtype=np.uint8), destination=filled,
              src_transform=src_transform, src_crs=src_crs, src_nodata=0,
              dst_transform=dst_transform, dst_crs=terrain_tiles.WM_CRS, dst_nodata=0,
              resampling=Resampling.nearest)
    crop = slice(_BUF, _BUF + TILE_PX)
    return rgb[:, crop, crop], filled[crop, crop].astype(bool)



def render_tile(items: list[Item], z: int, x: int, y: int) -> bytes:
    """Blocking: the JPEG for tile z/x/y from `items` (newest first), or b""
    if none of them has pixels there. Raises if a read fails."""
    bounds = terrain_tiles.tile_mercator_bounds(z, x, y)
    out = np.zeros((3, TILE_PX, TILE_PX), dtype=np.uint8)
    done = np.zeros((TILE_PX, TILE_PX), dtype=bool)
    with rasterio.Env(**GDAL_ENV, GDAL_HTTP_USERAGENT=user_agent()):
        for item in items:
            part = _read_item(item.href, bounds)
            if part is None:
                continue
            rgb, filled = part
            take = filled & ~done
            out[:, take] = rgb[:, take]
            done |= take
            if done.all():
                break
    if not done.any():
        return b""
    buf = io.BytesIO()
    Image.fromarray(np.moveaxis(out, 0, -1), mode="RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()
