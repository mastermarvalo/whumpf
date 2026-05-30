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

# Thread pool for blocking rasterio / PIL work — keeps asyncio event loop free.
# 2 workers: enough for terrain + slope concurrency without saturating a 6-core
# machine that may be running heavy pipeline jobs alongside the API.
_POOL = ThreadPoolExecutor(max_workers=2)

# Persistent HTTP client — reuses connections to TiTiler instead of opening a new
# TCP connection on every slope tile request.
_HTTP = httpx.AsyncClient(
    timeout=15.0,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
)

# ── in-memory LRU tile caches ──────────────────────────────────────────────────
# Both caches live in the asyncio event loop so no locking is needed.
# Average PNG sizes: slope ~25 KB, contour ~15 KB.

_SLOPE_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_CONTOUR_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_FILTER_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_TERRAIN_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_SLOPE_CACHE_MAX   = 512   # ~13 MB ceiling
_CONTOUR_CACHE_MAX = 1024  # ~15 MB ceiling
_FILTER_CACHE_MAX  = 512   # ~10 MB ceiling — outputs are mostly transparent
_TERRAIN_CACHE_MAX = 1024  # ~30 KB/tile (256×256 RGB PNG) → ~30 MB ceiling

# In-flight dedup for terrain tiles — if two requests for the same tile arrive
# before the first render completes, the second waits on the same Future instead
# of launching a duplicate render.  asyncio is single-threaded per uvicorn worker
# so this dict needs no lock.
_TERRAIN_IN_FLIGHT: dict[tuple, "asyncio.Future[bytes | None]"] = {}


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
    # vsicurl HTTP avoids GDAL's vsis3 driver, which breaks against MinIO
    # when GDAL_HTTP_VERSION=2 is set (MinIO doesn't support h2c).
    cog_url = (
        f"/vsicurl/{settings.s3_endpoint}"
        f"/{settings.s3_bucket_dem_cogs}/{region}/{cog_name}"
    )

    async def _do() -> httpx.Response:
        r = await _HTTP.get(
            f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png",
            params={
                "url": cog_url,
                "rescale": "0,60",
                "nodata": "-9999",
                "colormap_name": "caltopo_slope",
                # No buffer: TiTiler's `buffer` returns tilesize+2*buffer px of
                # neighbouring data, which MapLibre raster sources squeeze back
                # into the tile's own extent — drawing a duplicated strip of the
                # neighbour's slope at every seam. tilesize 512 still gives the
                # 2:1 downscale anti-aliasing.
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


# ── terrain filter ────────────────────────────────────────────────────────────
# Highlight slopes whose (aspect, slope-angle) match a user-selected combo —
# the single most important question for backcountry travel ("is this run a
# trap given today's CAIC problems?"). Read slope + aspect COGs at the tile
# bbox, mask, render a translucent PNG.

_ASPECT_CENTERS: dict[str, float] = {
    "N": 0, "NE": 45, "E": 90, "SE": 135,
    "S": 180, "SW": 225, "W": 270, "NW": 315,
}
_FILTER_RENDER_PX = 512
_FILTER_OUT_PX = 256
_FILTER_FILL_RGBA = (220, 50, 50, 180)  # avalanche-warning red, ~70% alpha


def _aspect_mask(aspect_deg: np.ndarray, allowed: set[str]) -> np.ndarray:
    """Boolean array: True where the cell's aspect falls in any selected bucket.

    Each bucket is a 45° arc centred on N/NE/E/…/NW. North wraps, so we
    compute angular distance via min(|d|, 360 - |d|).
    """
    if not allowed:
        return np.zeros_like(aspect_deg, dtype=bool)
    mask = np.zeros_like(aspect_deg, dtype=bool)
    for name in allowed:
        center = _ASPECT_CENTERS.get(name)
        if center is None:
            continue
        diff = np.abs(aspect_deg - center)
        diff = np.minimum(diff, 360 - diff)
        mask |= (diff <= 22.5)
    return mask


def _render_terrain_filter(
    slope_url: str,
    aspect_url: str,
    xmin: float, ymin: float, xmax: float, ymax: float,
    slope_min: float,
    slope_max: float,
    aspects: set[str],
) -> bytes | None:
    """Blocking: read slope + aspect at the tile window, mask, return PNG."""
    H = W = _FILTER_RENDER_PX
    dst_transform = from_bounds(xmin, ymin, xmax, ymax, W, H)

    slope_arr = np.full((H, W), -9999.0, dtype=np.float32)
    aspect_arr = np.full((H, W), -9999.0, dtype=np.float32)

    try:
        with rasterio.Env(**_COG_ENV):
            with rasterio.open(slope_url) as ds:
                reproject(
                    source=rasterio.band(ds, 1),
                    destination=slope_arr,
                    src_transform=ds.transform, src_crs=ds.crs,
                    src_nodata=ds.nodata,
                    dst_transform=dst_transform, dst_crs=_WM_CRS,
                    dst_nodata=-9999.0,
                    # MAX, not bilinear. When the destination cell is coarser
                    # than the source (zoomed out), bilinear averages slope
                    # across huge windows and an actual 35° rib gets smoothed
                    # into a 5° mean — the filter then highlights nothing. MAX
                    # preserves the steepest source slope in each cell, which
                    # is what we actually want for "does this area contain
                    # avalanche terrain at all?".
                    resampling=Resampling.max,
                )
            with rasterio.open(aspect_url) as ds:
                reproject(
                    source=rasterio.band(ds, 1),
                    destination=aspect_arr,
                    src_transform=ds.transform, src_crs=ds.crs,
                    src_nodata=ds.nodata,
                    dst_transform=dst_transform, dst_crs=_WM_CRS,
                    dst_nodata=-9999.0,
                    # Nearest for aspect — averaging directions gives meaningless
                    # numbers across the wrap-around at north. At low zooms a
                    # cell may contain mixed aspects we can't represent
                    # accurately, which is a known limitation; zooming in
                    # past z~13 gets you per-pixel-correct aspect filtering.
                    resampling=Resampling.nearest,
                )
    except Exception as exc:
        logger.warning("terrain_filter read failed: %s", exc)
        return None

    valid = (slope_arr != -9999.0) & (aspect_arr != -9999.0)
    matches = valid & (slope_arr >= slope_min) & (slope_arr <= slope_max) & _aspect_mask(aspect_arr, aspects)

    if not matches.any():
        return None  # empty tile → handler returns 204

    img = np.zeros((H, W, 4), dtype=np.uint8)
    img[matches] = list(_FILTER_FILL_RGBA)
    pil = Image.fromarray(img, mode="RGBA").resize((_FILTER_OUT_PX, _FILTER_OUT_PX), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


@router.get("/terrain_filter/{z}/{x}/{y}")
async def terrain_filter_tile(
    z: int, x: int, y: int,
    region: str = "colorado",
    slope_min: float = Query(default=30.0, ge=0, le=89),
    slope_max: float = Query(default=45.0, ge=1, le=90),
    aspects: str = Query(default="N,NE,E,SE,S,SW,W,NW"),
    hires: bool = False,
) -> Response:
    """Translucent overlay highlighting cells whose slope is in [slope_min,
    slope_max] AND aspect falls in any of `aspects` (comma-separated of
    N,NE,E,SE,S,SW,W,NW). Empty intersection → 204."""
    validate_region(region)
    if z < 0 or z > _MAX_Z or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(400, "tile coordinates out of range")
    if slope_min >= slope_max:
        raise HTTPException(400, "slope_min must be < slope_max")

    valid_aspect_names = set(_ASPECT_CENTERS)
    aspect_set = {a.strip().upper() for a in aspects.split(",") if a.strip()} & valid_aspect_names
    if not aspect_set:
        return Response(status_code=204)

    cache_key = (z, x, y, region, hires, round(slope_min, 1), round(slope_max, 1),
                 tuple(sorted(aspect_set)))
    if cached := _cache_get(_FILTER_CACHE, cache_key):
        return Response(content=cached, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})

    settings = get_settings()
    suffix = "_hires" if hires else ""
    base = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{region}"
    slope_url  = f"/vsicurl/{base}/slope{suffix}.tif"
    aspect_url = f"/vsicurl/{base}/aspect{suffix}.tif"

    xmin, ymin, xmax, ymax = _tile_mercator_bounds(z, x, y)

    loop = asyncio.get_event_loop()
    png = await loop.run_in_executor(
        _POOL, _render_terrain_filter,
        slope_url, aspect_url, xmin, ymin, xmax, ymax,
        slope_min, slope_max, aspect_set,
    )
    if png is None:
        return Response(status_code=204)

    _cache_put(_FILTER_CACHE, cache_key, png, _FILTER_CACHE_MAX)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# ── terrain-rgb tiles ──────────────────────────────────────────────────────────
# Serves terrarium-encoded RGB tiles for MapLibre's raster-dem terrain source.
# Encodes DEM COG data on-the-fly: elevation → R/G/B per terrarium spec
#   elevation = (R * 256 + G + B / 256) - 32768

_TERRAIN_PX  = 256  # MapLibre requests exactly 256×256 terrain tiles.
_TERRAIN_BUF = 1    # 1-px border overlap — prevents seams from independent bilinear resampling.


def _render_terrain_rgb(
    dem_url: str,
    xmin: float, ymin: float, xmax: float, ymax: float,
) -> bytes | None:
    """Blocking: read DEM window and encode as terrarium-RGB PNG.

    Renders at (_TERRAIN_PX + 2*_TERRAIN_BUF)² then crops to _TERRAIN_PX².
    The extra border pixels ensure bilinear resampling at tile edges uses
    data from the neighbouring tile's extent, eliminating seam artifacts.
    """
    buf   = _TERRAIN_BUF
    total = _TERRAIN_PX + 2 * buf
    pw = (xmax - xmin) / _TERRAIN_PX  # mercator units per pixel
    ph = (ymax - ymin) / _TERRAIN_PX
    xmin_b, ymin_b = xmin - buf * pw, ymin - buf * ph
    xmax_b, ymax_b = xmax + buf * pw, ymax + buf * ph

    data = np.full((total, total), np.nan, dtype=np.float32)
    dst_transform = from_bounds(xmin_b, ymin_b, xmax_b, ymax_b, total, total)
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
                    dst_nodata=np.nan,
                    resampling=Resampling.bilinear,
                )
    except Exception as exc:
        logger.debug("terrain_rgb DEM read failed: %s", exc)
        return None

    # Crop border pixels back to 256×256.
    data = data[buf:buf + _TERRAIN_PX, buf:buf + _TERRAIN_PX]

    if np.all(np.isnan(data)):
        return None  # no valid pixels — let fallback handle it

    elev    = np.where(np.isnan(data), 0.0, data).astype(np.float64)
    val     = np.clip(elev + 32768.0, 0.0, 65535.0)
    int_val = np.floor(val).astype(np.uint32)
    r = (int_val >> 8).astype(np.uint8)
    g = (int_val & 0xFF).astype(np.uint8)
    b = np.floor((val - np.floor(val)) * 256.0).astype(np.uint8)

    img = np.stack([r, g, b], axis=-1)
    out = io.BytesIO()
    Image.fromarray(img, mode="RGB").save(out, format="PNG", compress_level=1)
    return out.getvalue()


def _render_terrain_rgb_with_fallback(
    dem_url: str, fallback_url: str | None,
    xmin: float, ymin: float, xmax: float, ymax: float,
) -> bytes | None:
    """Try dem_url; on None result retry with fallback_url."""
    result = _render_terrain_rgb(dem_url, xmin, ymin, xmax, ymax)
    if result is None and fallback_url:
        result = _render_terrain_rgb(fallback_url, xmin, ymin, xmax, ymax)
    return result


@router.get("/terrain_rgb/{z}/{x}/{y}")
async def terrain_rgb_tile(
    z: int, x: int, y: int,
    region: str = "colorado",
) -> Response:
    """Terrarium-encoded RGB tile for MapLibre 3D terrain.

    z≤12 → 10m DEM; z≥13 → 1m hires with 10m fallback.
    """
    validate_region(region)
    if z < 0 or z > _MAX_Z or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(400, "tile coordinates out of range")

    cache_key = (z, x, y, region)
    if cached := _cache_get(_TERRAIN_CACHE, cache_key):
        return Response(content=cached, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    # If this tile is already being rendered, wait on the existing Future instead
    # of launching a duplicate render (prevents N×CPU burn for burst requests).
    if cache_key in _TERRAIN_IN_FLIGHT:
        png = await asyncio.shield(_TERRAIN_IN_FLIGHT[cache_key])
        if png is None:
            return Response(status_code=204)
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    settings = get_settings()
    xmin, ymin, xmax, ymax = _tile_mercator_bounds(z, x, y)

    use_hires    = z >= 13
    dem_url      = _build_dem_url(settings, region, hires=use_hires)
    fallback_url = _build_dem_url(settings, region, hires=False) if use_hires else None

    loop = asyncio.get_event_loop()
    fut: asyncio.Future[bytes | None] = loop.run_in_executor(
        _POOL, _render_terrain_rgb_with_fallback,
        dem_url, fallback_url, xmin, ymin, xmax, ymax,
    )
    _TERRAIN_IN_FLIGHT[cache_key] = fut
    try:
        png = await fut
    finally:
        _TERRAIN_IN_FLIGHT.pop(cache_key, None)

    if png is None:
        return Response(status_code=204)

    _cache_put(_TERRAIN_CACHE, cache_key, png, _TERRAIN_CACHE_MAX)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )
