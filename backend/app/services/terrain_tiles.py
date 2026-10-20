"""Tile code shared by the live tile endpoints (routers/tiles.py) and the
offline pack cache (services/pack_cache.py), so a pack tile and a live tile
always come from the same request or render and decode to the same values.

- Slope and aspect: the TiTiler request (colormap_request, fetch_colormap)
  and the palette PNG re-encode (to_palette_png).
- Elevation: the Terrarium render (render_terrain_rgb, with_fallback). The
  live endpoint encodes at PNG level 1 for speed, packs at level 9; the
  pixels are the same.
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable
from typing import Literal

import httpx
import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from app.http_retry import call_with_resilience

# Same logger as the endpoints, so their log lines read as before.
logger = logging.getLogger("whumpf.tiles")

# ── coordinate helpers ─────────────────────────────────────────────────────────

WM_CRS = CRS.from_epsg(3857)
_WGS84_TO_WM = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)


def tile_mercator_bounds(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Return (xmin, ymin, xmax, ymax) in EPSG:3857 for a standard XYZ tile."""
    n = 2 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * (y + 1) / n))))
    xmin, ymin = _WGS84_TO_WM.transform(west, south)
    xmax, ymax = _WGS84_TO_WM.transform(east, north)
    return xmin, ymin, xmax, ymax


# Rasterio GDAL env vars for vsicurl access to the MinIO public bucket.
COG_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_USE_HEAD": "FALSE",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
}


def build_dem_url(settings, region: str, hires: bool = False) -> str:
    """vsicurl HTTP URL for a DEM COG in MinIO — avoids S3 credential issues."""
    name = "dem_hires.tif" if hires else "dem.tif"
    return f"/vsicurl/{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/{region}/{name}"


# ── slope / aspect (TiTiler, colormapped) ──────────────────────────────────────

ColormapLayer = Literal["slope", "aspect"]


def colormap_cog(layer: ColormapLayer, hires: bool = False) -> str:
    """The COG a slope or aspect tile is rendered from (aspect has no hires)."""
    if layer == "slope":
        return "slope_hires.tif" if hires else "slope.tif"
    return "aspect.tif"


def colormap_request(
    settings, layer: ColormapLayer, region: str, z: int, x: int, y: int, hires: bool = False,
) -> tuple[str, dict]:
    """The TiTiler URL and parameters for a slope or aspect tile."""
    # vsicurl HTTP avoids GDAL's vsis3 driver, which breaks against MinIO
    # when GDAL_HTTP_VERSION=2 is set (MinIO doesn't support h2c).
    cog_url = (
        f"/vsicurl/{settings.s3_endpoint}"
        f"/{settings.s3_bucket_dem_cogs}/{region}/{colormap_cog(layer, hires)}"
    )
    url = f"{settings.titiler_url}/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    if layer == "slope":
        # CalTopo V1 slope colormap is pre-built as data/colormaps/caltopo_slope.npy
        # and registered with TiTiler via COLORMAP_DIRECTORY. Reference it by name
        # to avoid embedding ~2 KB of JSON in every tile request URL.
        params = {
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
        }
    else:
        params = {
            "url": cog_url,
            "rescale": "0,360",
            "nodata": "-9999",
            "colormap_name": "hsv",
            "tilesize": 512,
        }
    return url, params


async def fetch_colormap(
    client: httpx.AsyncClient, settings, layer: ColormapLayer, region: str,
    z: int, x: int, y: int, hires: bool = False,
) -> httpx.Response:
    """GET a slope or aspect tile from TiTiler under retry and the "titiler"
    breaker. A 404 (tile outside the COG) is returned, not raised; other
    errors raise as call_with_resilience does."""
    url, params = colormap_request(settings, layer, region, z, x, y, hires)

    async def _do() -> httpx.Response:
        r = await client.get(url, params=params)
        # 404 is "tile outside COG extent" — a normal empty-tile response.
        # Don't raise so retry/breaker treat it as success.
        if r.status_code != 404:
            r.raise_for_status()
        return r

    return await call_with_resilience("titiler", _do)


def to_palette_png(png: bytes) -> bytes:
    """Blocking: re-encode a colormapped RGBA tile from TiTiler as a palette PNG.

    A colormapped tile has at most 256 colours (the caltopo_slope ramp has 256,
    its entry 0 being the transparent nodata colour; hsv has 256, of which a
    0–360 rescale reaches 255, plus transparent nodata). Pixel-identical at
    roughly half the size. Fully transparent pixels are first set to
    (0,0,0,0), which looks the same. Over 256 colours, or if the palette PNG
    isn't smaller (some flat plains tiles), the input is returned unchanged;
    never quantized.
    """
    rgba = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA")).copy()
    rgba[rgba[..., 3] == 0] = 0
    colours, index = np.unique(rgba.view(np.uint32).ravel(), return_inverse=True)
    if len(colours) > 256:
        logger.debug("tile has %d colours, serving it as RGBA", len(colours))
        return png
    palette = colours.view(np.uint8).reshape(-1, 4)
    height, width = rgba.shape[:2]
    out = Image.frombytes("P", (width, height), index.astype(np.uint8).tobytes())
    out.putpalette(palette[:, :3].tobytes())
    out.info["transparency"] = palette[:, 3].tobytes()
    buf = io.BytesIO()
    # Level 1 comes out slightly smaller than 6 or 9 on these tiles, and faster.
    out.save(buf, format="PNG", compress_level=1)
    return buf.getvalue() if buf.tell() < len(png) else png


# ── elevation (Terrarium RGB) ──────────────────────────────────────────────────
# Encodes DEM COG data: elevation → R/G/B per terrarium spec
#   elevation = (R * 256 + G + B / 256) - 32768

TERRAIN_PX  = 256  # MapLibre requests exactly 256×256 terrain tiles.
TERRAIN_BUF = 1    # 1-px border overlap — prevents seams from independent bilinear resampling.

# A failed or timed-out read raises, so it never looks like "no data".
TERRAIN_COG_ENV = dict(COG_ENV, GDAL_HTTP_TIMEOUT="10")


def _flat_terrain_png() -> bytes:
    """256×256 tile at 0 m. MapLibre's raster-dem needs every tile the same
    size, so empty areas get this instead of a 204 ("dem dimension mismatch").
    Terrarium: 0 m → 0 + 32768 = 128*256 + 0 + 0/256 → (128, 0, 0)."""
    out = io.BytesIO()
    Image.new("RGB", (TERRAIN_PX, TERRAIN_PX), (128, 0, 0)).save(out, format="PNG")
    return out.getvalue()


FLAT_TERRAIN_PNG = _flat_terrain_png()


def render_terrain_rgb(
    dem_url: str,
    xmin: float, ymin: float, xmax: float, ymax: float,
    compress_level: int = 1,
) -> bytes | None:
    """Blocking: read DEM window and encode as terrarium-RGB PNG.

    Returns None when the window has no valid pixels; raises if the read fails.
    Renders at (TERRAIN_PX + 2*TERRAIN_BUF)² then crops to TERRAIN_PX².
    The extra border pixels ensure bilinear resampling at tile edges uses
    data from the neighbouring tile's extent, eliminating seam artifacts.
    """
    buf   = TERRAIN_BUF
    total = TERRAIN_PX + 2 * buf
    pw = (xmax - xmin) / TERRAIN_PX  # mercator units per pixel
    ph = (ymax - ymin) / TERRAIN_PX
    xmin_b, ymin_b = xmin - buf * pw, ymin - buf * ph
    xmax_b, ymax_b = xmax + buf * pw, ymax + buf * ph

    data = np.full((total, total), np.nan, dtype=np.float32)
    dst_transform = from_bounds(xmin_b, ymin_b, xmax_b, ymax_b, total, total)
    with rasterio.Env(**TERRAIN_COG_ENV), rasterio.open(dem_url) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=data,
            src_transform=src.transform,
            src_crs=src.crs,
            src_nodata=src.nodata,
            dst_transform=dst_transform,
            dst_crs=WM_CRS,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )

    # Crop border pixels back to 256×256.
    data = data[buf:buf + TERRAIN_PX, buf:buf + TERRAIN_PX]

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
    Image.fromarray(img, mode="RGB").save(out, format="PNG", compress_level=compress_level)
    return out.getvalue()


def with_fallback(
    render: Callable[[str], bytes | None], dem_url: str, fallback_url: str | None,
) -> bytes | None:
    """Try render(dem_url); on no data or a read error retry with fallback_url.

    Returns None when neither has data; raises if the last read fails.
    """
    try:
        result = render(dem_url)
    except Exception as exc:
        if not fallback_url:
            raise
        logger.warning("terrain_rgb hires read failed, trying 10m: %s", exc)
        result = None
    if result is None and fallback_url:
        result = render(fallback_url)
    return result


def terrain_urls(settings, region: str, z: int) -> tuple[str, str | None]:
    """(DEM url, fallback url) for an elevation tile: z≤12 → 10m DEM;
    z≥13 → 1m hires with 10m fallback."""
    if z >= 13:
        return build_dem_url(settings, region, hires=True), build_dem_url(settings, region, hires=False)
    return build_dem_url(settings, region, hires=False), None


def render_terrain_rgb_with_fallback(
    dem_url: str, fallback_url: str | None,
    xmin: float, ymin: float, xmax: float, ymax: float,
    compress_level: int = 1,
) -> bytes | None:
    """Blocking: render_terrain_rgb with the hires → 10m fallback."""
    return with_fallback(
        lambda url: render_terrain_rgb(url, xmin, ymin, xmax, ymax, compress_level),
        dem_url, fallback_url,
    )
