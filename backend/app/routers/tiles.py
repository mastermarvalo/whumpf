"""Tile proxy — server-side colormaps and contour rendering for layers
TiTiler can't handle natively."""

from __future__ import annotations

import asyncio
import io
import logging
import math
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import httpx
import numpy as np
import rasterio
from contourpy import contour_generator
from fastapi import APIRouter, HTTPException, Query, Response
from PIL import Image, ImageDraw
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from app.config import get_settings, validate_region
from app.http_retry import CircuitOpenError, call_with_resilience

# Web-mercator tile coordinates are bounded by z (we serve at most z16) and
# 0 <= x,y < 2**z. Validating up-front avoids upstream rasterio reads for
# nonsensical inputs.
_MAX_Z = 18

router = APIRouter(prefix="/tiles", tags=["tiles"])
logger = logging.getLogger("whumpf.tiles")

_BUCKET = "dem-cogs"

# Thread pool for blocking rasterio / PIL work — keeps asyncio event loop free.
_POOL = ThreadPoolExecutor(max_workers=6)

# Persistent HTTP client — reuses connections to TiTiler instead of opening a new
# TCP connection on every slope tile request.
_HTTP = httpx.AsyncClient(
    timeout=15.0,
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)

# ── in-memory LRU tile caches ──────────────────────────────────────────────────
# Both caches live in the asyncio event loop so no locking is needed.
# Average PNG sizes: slope ~25 KB, contour ~15 KB.

_SLOPE_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_CONTOUR_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_SLOPE_CACHE_MAX   = 512   # ~13 MB ceiling
_CONTOUR_CACHE_MAX = 1024  # ~15 MB ceiling


def _cache_get(store: "OrderedDict[tuple, bytes]", key: tuple) -> bytes | None:
    if key not in store:
        return None
    store.move_to_end(key)
    return store[key]


def _cache_put(store: "OrderedDict[tuple, bytes]", key: tuple, val: bytes, limit: int) -> None:
    if key in store:
        store.move_to_end(key)
    else:
        store[key] = val
        if len(store) > limit:
            store.popitem(last=False)

# ── coordinate helpers ─────────────────────────────────────────────────────────

_WM_CRS = CRS.from_epsg(3857)
_WGS84_TO_WM = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def _tile_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) in EPSG:3857 for a standard XYZ tile."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    xmin, ymin = _WGS84_TO_WM.transform(west, south)
    xmax, ymax = _WGS84_TO_WM.transform(east, north)
    return xmin, ymin, xmax, ymax


# ── slope colormap ─────────────────────────────────────────────────────────────

def _s3(path: str) -> str:
    return f"s3://{_BUCKET}/{path}"


# CalTopo V1 slope colormap is pre-built as data/colormaps/caltopo_slope.npy and
# registered with TiTiler via COLORMAP_DIRECTORY. Reference it by name to avoid
# embedding ~2 KB of JSON in every tile request URL.


@router.get("/slope/{z}/{x}/{y}")
async def slope_tile(
    z: int, x: int, y: int,
    region: str = "colorado",
    hires: bool = False,
) -> Response:
    """Proxy a slope tile from TiTiler with the CalTopo V1 color ramp.

    hires=true serves from slope_hires.tif (1m priority-stack derivative).
    Requests 512×512 from TiTiler so MapLibre's 2:1 display downscale
    acts as bilinear anti-aliasing, reducing the blocky DEM cell appearance.
    """
    validate_region(region)
    if z < 0 or z > _MAX_Z or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(400, "tile coordinates out of range")
    cog_name = "slope_hires.tif" if hires else "slope.tif"

    cache_key = (z, x, y, region, hires)
    if cached := _cache_get(_SLOPE_CACHE, cache_key):
        return Response(content=cached, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    settings = get_settings()

    async def _do() -> httpx.Response:
        r = await _HTTP.get(
            f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
            params={
                "url": _s3(f"{region}/{cog_name}"),
                "rescale": "0,60",
                "nodata": "-9999",
                "colormap_name": "caltopo_slope",
                "buffer": 2,
                "tilesize": 512,
            },
        )
        # 404 is "tile outside COG extent" — a normal empty-tile response.
        # Don't raise so retry/breaker treat it as success.
        if r.status_code != 404:
            r.raise_for_status()
        return r

    try:
        resp = await call_with_resilience("titiler", _do)
    except CircuitOpenError:
        raise HTTPException(503, "tile server temporarily unavailable")
    except httpx.RequestError as exc:
        logger.error("TiTiler unreachable: %s", exc)
        raise HTTPException(502, "tile server unreachable") from exc
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "TiTiler returned %d for slope tile %d/%d/%d",
            exc.response.status_code, z, x, y,
        )
        raise HTTPException(502, f"TiTiler returned {exc.response.status_code}") from exc

    if resp.status_code == 404:
        return Response(status_code=204)

    _cache_put(_SLOPE_CACHE, cache_key, resp.content, _SLOPE_CACHE_MAX)
    return Response(
        content=resp.content,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── contour tiles ──────────────────────────────────────────────────────────────

# Render at 2× pixel density and LANCZOS-downsample for anti-aliasing.
_RENDER_PX = 512
_OUT_PX = 256

# Rasterio GDAL env vars for vsicurl access to the MinIO public bucket.
_COG_ENV = dict(
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_USE_HEAD="FALSE",
    GDAL_HTTP_MULTIPLEX="YES",
    GDAL_HTTP_VERSION="2",
)

# Minor line: dark, semi-transparent, thin.  Major: darker, opaque-ish, thicker.
# Pixel widths are at _RENDER_PX resolution (halved after LANCZOS downsample).
_MINOR_COLOR = (30, 30, 30, 110)
_MAJOR_COLOR = (10, 10, 10, 200)
_MINOR_WIDTH = 2   # px at 2x = ~1px displayed
_MAJOR_WIDTH = 4   # px at 2x = ~2px displayed


def _contour_intervals(z: int) -> tuple[float, float]:
    """Return (minor_m, major_m) contour intervals for a given zoom level.

    z >= 14  →  12m / 60m   (~40ft/200ft, CalTopo planning resolution)
    z 11-13  →  40m / 200m  (~130ft/660ft, overview)
    z < 11   →  100m only   (major-only; minor == major so minor_levels is empty)
    """
    if z >= 14:
        return 12.0, 60.0
    if z >= 11:
        return 40.0, 200.0
    return 100.0, 100.0


def _build_dem_url(settings, region: str, hires: bool = False) -> str:
    """vsicurl HTTP URL for a DEM COG in MinIO — avoids S3 credential issues."""
    name = "dem_hires.tif" if hires else "dem.tif"
    return f"/vsicurl/{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{region}/{name}"


def _render_contours(
    dem_url: str, xmin: float, ymin: float, xmax: float, ymax: float, z: int,
    interval_m: float | None = None,
) -> bytes | None:
    """Blocking: read DEM window, generate contours, return PNG bytes.

    interval_m overrides the zoom-adaptive interval when set (minor = interval_m,
    major = interval_m * 5).
    """
    if interval_m is not None:
        minor_m, major_m = interval_m, interval_m * 5.0
    else:
        minor_m, major_m = _contour_intervals(z)

    data = np.full((_RENDER_PX, _RENDER_PX), -9999.0, dtype=np.float32)
    dst_transform = from_bounds(xmin, ymin, xmax, ymax, _RENDER_PX, _RENDER_PX)

    try:
        with rasterio.Env(**_COG_ENV):
            with rasterio.open(dem_url) as src:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=data,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    src_nodata=src.nodata,
                    dst_transform=dst_transform,
                    dst_crs=_WM_CRS,
                    dst_nodata=-9999.0,
                    resampling=Resampling.bilinear,
                )
    except Exception as exc:
        logger.debug("contour DEM read failed: %s", exc)
        return None

    valid = data[data != -9999.0]
    if valid.size == 0:
        return None

    elev_min, elev_max = float(valid.min()), float(valid.max())
    if elev_max - elev_min < minor_m:
        return None  # flat tile — no contours to draw

    # Replace nodata with NaN so contourpy skips those regions.
    grid = np.where(data != -9999.0, data, np.nan)

    # Build level lists. When minor_m == major_m (z < 11) every level lands in
    # major_set so minor_levels is empty and only thick lines are drawn.
    minor_start = math.ceil(elev_min / minor_m) * minor_m
    all_levels = np.arange(minor_start, elev_max, minor_m).tolist()
    major_start = math.ceil(elev_min / major_m) * major_m
    major_set = {round(major_start + k * major_m, 6) for k in range(int((elev_max - elev_min) / major_m) + 2)}
    minor_levels = [l for l in all_levels if round(l, 6) not in major_set]
    major_levels = [l for l in all_levels if round(l, 6) in major_set]

    # contourpy: grid[row, col] with row=0 at top (north), col=0 at left (west).
    # Output line coordinates are (col, row) — directly usable as PIL (x, y).
    gen = contour_generator(z=grid)

    img = Image.new("RGBA", (_RENDER_PX, _RENDER_PX), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for lvl in minor_levels:
        for seg in gen.lines(lvl):
            pts = [tuple(map(float, p)) for p in seg]
            if len(pts) >= 2:
                draw.line(pts, fill=_MINOR_COLOR, width=_MINOR_WIDTH)

    for lvl in major_levels:
        for seg in gen.lines(lvl):
            pts = [tuple(map(float, p)) for p in seg]
            if len(pts) >= 2:
                draw.line(pts, fill=_MAJOR_COLOR, width=_MAJOR_WIDTH)

    out = img.resize((_OUT_PX, _OUT_PX), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    return buf.getvalue()


def _render_contours_with_fallback(
    dem_url: str, fallback_url: str | None,
    xmin: float, ymin: float, xmax: float, ymax: float, z: int,
    interval_m: float | None = None,
) -> bytes | None:
    """Try dem_url; if rasterio fails to open it, retry with fallback_url."""
    try:
        return _render_contours(dem_url, xmin, ymin, xmax, ymax, z, interval_m)
    except Exception:
        if fallback_url:
            return _render_contours(fallback_url, xmin, ymin, xmax, ymax, z, interval_m)
        return None


@router.get("/contours/{z}/{x}/{y}")
async def contour_tile(
    z: int, x: int, y: int,
    region: str = "colorado",
    interval: float | None = Query(None, description="Contour interval in metres (None = zoom-adaptive)"),
) -> Response:
    """Render zoom-adaptive DEM contour lines as a transparent PNG tile.

    z<11: 100m major only  |  z11-13: 40m/200m  |  z>=14: 12m/60m (CalTopo quality)

    At z>=13, prefers dem_hires.tif (1m) over dem.tif (10m) when hires data
    exists for the region. Falls back to dem.tif transparently if hires is absent.
    """
    validate_region(region)
    if z < 0 or z > _MAX_Z or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(400, "tile coordinates out of range")
    cache_key = (z, x, y, region, interval)
    if cached := _cache_get(_CONTOUR_CACHE, cache_key):
        return Response(content=cached, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    settings = get_settings()
    xmin, ymin, xmax, ymax = _tile_mercator_bounds(z, x, y)

    base_url  = _build_dem_url(settings, region, hires=False)
    hires_url = _build_dem_url(settings, region, hires=True) if z >= 13 else None

    if hires_url:
        dem_url      = hires_url
        fallback_url = base_url
    else:
        dem_url      = base_url
        fallback_url = None

    loop = asyncio.get_event_loop()
    png = await loop.run_in_executor(
        _POOL, _render_contours_with_fallback,
        dem_url, fallback_url, xmin, ymin, xmax, ymax, z, interval,
    )
    if png is None:
        return Response(status_code=204)

    _cache_put(_CONTOUR_CACHE, cache_key, png, _CONTOUR_CACHE_MAX)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
