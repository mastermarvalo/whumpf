"""Regression guard for slope tile sizing.

The slope layer is served as 512×512 tiles for 2:1 downscale anti-aliasing, and
must NOT use TiTiler's `buffer` param: `buffer` returns tilesize+2*buffer px of
neighbouring data, which a MapLibre raster source redraws inside the tile's own
extent — duplicating a strip of the neighbour's slope at every tile seam.
"""

from __future__ import annotations

import asyncio
import struct
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from PIL import Image
from rasterio.enums import Resampling
from rasterio.transform import from_bounds

from app.main import app
from app.routers import tiles
from app.services import terrain_tiles


def _png_bytes(w: int, h: int) -> bytes:
    buf = BytesIO()
    Image.new("RGBA", (w, h)).save(buf, "PNG")
    return buf.getvalue()


def test_slope_tile_is_512_and_unbuffered(monkeypatch):
    captured: dict = {}

    async def fake_get(url, params=None, **kwargs):
        captured["url"] = url
        captured["params"] = params or {}
        # raise_for_status() in the endpoint needs a bound request.
        return httpx.Response(
            200, content=_png_bytes(512, 512),
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url, params=params),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)

    client = TestClient(app)
    resp = client.get("/tiles/slope/10/210/394?region=colorado")
    assert resp.status_code == 200

    # The invariants that prevent duplicated slope at tile borders:
    assert str(captured["params"].get("tilesize")) == "512"
    assert "buffer" not in captured["params"], "buffer bakes neighbour pixels into the tile"

    # And the served PNG is genuinely 512×512 (not tilesize + 2*buffer).
    width, height = struct.unpack(">II", resp.content[16:24])
    assert (width, height) == (512, 512)
    assert resp.content == tiles._to_palette_png(_png_bytes(512, 512))


def test_aspect_tile_uses_hsv_and_caches(monkeypatch):
    captured: dict = {}

    async def fake_get(url, params=None, **kwargs):
        captured["params"] = params or {}
        return httpx.Response(
            200, content=_png_bytes(512, 512),
            headers={"content-type": "image/png"},
            request=httpx.Request("GET", url, params=params),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)

    client = TestClient(app)
    resp = client.get("/tiles/aspect/10/211/395?region=colorado")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == tiles._to_palette_png(_png_bytes(512, 512))

    params = captured["params"]
    assert params["colormap_name"] == "hsv"
    assert params["rescale"] == "0,360"
    assert str(params["tilesize"]) == "512"
    assert "buffer" not in params
    assert params["url"].endswith("/colorado/aspect.tif")

    # Out-of-range tile coords are rejected.
    assert client.get("/tiles/aspect/1/5/0?region=colorado").status_code == 400


# ── slope/aspect palette PNGs: pixel-identical, never quantized ─────────────

# caltopo_slope.npy is a copy of data/colormaps/caltopo_slope.npy (TiTiler's
# COLORMAP_DIRECTORY); titiler_hsv.npy is rio-tiler's "hsv" (rio-tiler 9.0.6).
_DATA = Path(__file__).parent / "data"


def _colormapped_tile(colormap: np.ndarray, nodata_rgb: tuple[int, int, int]) -> np.ndarray:
    """512×512 RGBA tile using every colormap entry, plus a band of transparent
    nodata pixels whose RGB is `nodata_rgb` (as TiTiler masks them)."""
    # Shuffled so it doesn't compress unrealistically well as RGBA.
    values = np.random.default_rng(0).permutation(512 * 512) % len(colormap)
    rgba = colormap[values].reshape(512, 512, 4).copy()
    rgba[:16] = (*nodata_rgb, 0)
    return rgba


def _png(rgba: np.ndarray) -> bytes:
    buf = BytesIO()
    Image.fromarray(rgba).save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("colormap_file, entries, nodata_rgb", [
    # Slope: all 256 entries; nodata rescales to entry 0, already (0,0,0,0).
    ("caltopo_slope.npy", 256, (0, 0, 0)),
    # Aspect: a 0–360 rescale reaches 255 entries, and masked pixels take
    # entry 0's red with alpha 0.
    ("titiler_hsv.npy", 255, (255, 0, 0)),
])
def test_palette_png_is_pixel_identical(colormap_file, entries, nodata_rgb):
    colormap = np.load(_DATA / colormap_file)[:entries]
    rgba = _colormapped_tile(colormap, nodata_rgb)
    original = _png(rgba)

    out = tiles._to_palette_png(original)

    img = Image.open(BytesIO(out))
    assert img.mode == "P"
    decoded = np.asarray(img.convert("RGBA"))
    expected = rgba.copy()
    expected[expected[..., 3] == 0] = 0  # invisible either way
    assert np.array_equal(decoded, expected)


def test_palette_png_falls_back_over_256_colours():
    colormap = np.load(_DATA / "titiler_hsv.npy")  # 256 opaque colours
    rgba = _colormapped_tile(colormap, (0, 0, 0))  # + transparent = 257
    original = _png(rgba)
    assert tiles._to_palette_png(original) == original


def test_palette_png_keeps_original_when_not_smaller():
    # A single-colour tile is already tiny as RGBA.
    original = _png(np.full((512, 512, 4), (200, 30, 30, 255), dtype=np.uint8))
    out = tiles._to_palette_png(original)
    assert len(out) <= len(original)
    assert np.array_equal(np.asarray(Image.open(BytesIO(out)).convert("RGBA")),
                          np.asarray(Image.open(BytesIO(original)).convert("RGBA")))


# ── terrain_rgb: no data → flat 0 m tile, read failure → 503 no-store ────────

_TERRAIN_URL = "/tiles/terrain_rgb/10/210/394?region=colorado"
_HIRES_URL = "/tiles/terrain_rgb/13/1683/3155?region=colorado"


@pytest.fixture
def terrain_cache(monkeypatch):
    cache: OrderedDict = OrderedDict()
    monkeypatch.setattr(tiles, "_TERRAIN_CACHE", cache)
    monkeypatch.setattr(tiles, "_TERRAIN_IN_FLIGHT", {})
    return cache


def _terrarium_m(png: bytes) -> np.ndarray:
    px = np.asarray(Image.open(BytesIO(png)).convert("RGB"), dtype=np.float64)
    return px[..., 0] * 256 + px[..., 1] + px[..., 2] / 256 - 32768


def test_flat_terrain_tile_is_256_and_zero_metres():
    img = Image.open(BytesIO(tiles._FLAT_TERRAIN_PNG))
    assert img.size == (256, 256)
    assert set(img.convert("RGB").getdata()) == {(128, 0, 0)}
    assert np.all(_terrarium_m(tiles._FLAT_TERRAIN_PNG) == 0.0)


def test_terrain_no_data_returns_flat_tile(monkeypatch, terrain_cache):
    monkeypatch.setattr(tiles, "_render_terrain_rgb", lambda *a: None)
    resp = TestClient(app).get(_TERRAIN_URL)
    assert resp.status_code == 200
    assert resp.content == tiles._FLAT_TERRAIN_PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_terrain_read_failure_is_503_no_store_and_not_cached(monkeypatch, terrain_cache):
    def boom(*a):
        raise RuntimeError("vsicurl read failed")

    monkeypatch.setattr(tiles, "_render_terrain_rgb", boom)
    client = TestClient(app)
    resp = client.get(_TERRAIN_URL)
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"
    assert len(terrain_cache) == 0
    assert tiles._TERRAIN_IN_FLIGHT == {}

    # The next request renders again instead of replaying the failure.
    real = _png_bytes(256, 256)
    monkeypatch.setattr(tiles, "_render_terrain_rgb", lambda *a: real)
    resp = client.get(_TERRAIN_URL)
    assert resp.status_code == 200
    assert resp.content == real
    assert list(terrain_cache.values()) == [real]


def test_terrain_read_timeout_is_503_no_store(monkeypatch, terrain_cache):
    monkeypatch.setattr(tiles, "_TERRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tiles, "_render_terrain_rgb", lambda *a: time.sleep(0.3) or b"late")
    resp = TestClient(app).get(_TERRAIN_URL)
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"
    assert len(terrain_cache) == 0


def test_terrain_waiter_on_failed_render_gets_503(terrain_cache):
    async def run():
        fut = asyncio.get_running_loop().create_future()
        fut.set_exception(RuntimeError("read failed"))
        tiles._TERRAIN_IN_FLIGHT[(10, 210, 394, "colorado")] = fut
        return await tiles.terrain_rgb_tile(10, 210, 394, region="colorado")

    resp = asyncio.run(run())
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"
    assert len(terrain_cache) == 0


def test_terrain_hires_failure_falls_back_to_10m(monkeypatch, terrain_cache):
    real = _png_bytes(256, 256)

    def render(dem_url, *a):
        if dem_url == "hires.tif":
            raise RuntimeError("hires read failed")
        return real

    monkeypatch.setattr(tiles, "_render_terrain_rgb", render)
    calls = []
    monkeypatch.setattr(tiles, "_build_dem_url",
                        lambda s, region, hires=False: calls.append(hires) or ("hires.tif" if hires else "10m.tif"))
    resp = TestClient(app).get(_HIRES_URL)
    assert resp.status_code == 200
    assert resp.content == real
    assert calls == [True, False]


# ── terrain at low zoom: overviews, z<8 cut-off, no duplicate renders ────────

@pytest.mark.parametrize("src_res, z, expected", [
    (11.607, 8, 4),     # the 10m DEM at z8 (~611 m/px): the 32× overview (371 m)
    (11.607, 12, 0),    # z12 (~38 m/px): the 2× overview (23 m)
    (11.607, 13, None), # z13 (~19 m/px): full resolution
    (1.2526, 13, 2),    # the 1m DEM at z13: the 8× overview (10 m)
    (1.2526, 14, 1),    # and at z14 (~9.5 m/px): the 4× overview (5 m)
])
def test_overview_level_is_coarsest_still_as_fine_as_the_tile(src_res, z, expected):
    xmin, _, xmax, _ = terrain_tiles.tile_mercator_bounds(z, 0, 0)
    dst_res = (xmax - xmin) / terrain_tiles.TERRAIN_PX
    assert terrain_tiles.overview_level(src_res, dst_res, [2, 4, 8, 16, 32, 64]) == expected


def test_overview_level_without_overviews_is_full_resolution():
    assert terrain_tiles.overview_level(10.0, 600.0, []) is None


_Z8 = (8, 52, 97)  # Berthoud Pass / Front Range
_SRC_RES = 60.0


@pytest.fixture(scope="module")
def z8_cog(tmp_path_factory) -> Path:
    """A 3857 COG-like GeoTIFF over tile 8/52/97 at 60 m with 2–32× overviews.
    Elevation is a plane, so any overview of it decodes to the same values."""
    xmin, ymin, xmax, ymax = terrain_tiles.tile_mercator_bounds(*_Z8)
    pad = (xmax - xmin) * 0.01
    left, top = xmin - pad, ymax + pad
    w = int((xmax - xmin + 2 * pad) / _SRC_RES)
    h = int((ymax - ymin + 2 * pad) / _SRC_RES)
    rows, cols = np.mgrid[0:h, 0:w].astype(np.float32)
    elev = 2000 + 0.4 * cols + 0.2 * rows
    path = tmp_path_factory.mktemp("z8") / "dem.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
        crs="EPSG:3857", nodata=-9999, tiled=True, blockxsize=512, blockysize=512,
        compress="deflate",
        transform=from_bounds(left, top - h * _SRC_RES, left + w * _SRC_RES, top, w, h),
    ) as dst:
        dst.write(elev, 1)
        dst.build_overviews([2, 4, 8, 16, 32], Resampling.average)
    return path


def test_z8_terrain_renders_in_seconds_from_the_matching_overview(z8_cog, monkeypatch):
    opened: list[dict] = []
    real_open = rasterio.open

    def spy_open(path, *args, **kwargs):
        opened.append(kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(terrain_tiles.rasterio, "open", spy_open)
    bounds = terrain_tiles.tile_mercator_bounds(*_Z8)

    t0 = time.monotonic()
    png = terrain_tiles.render_terrain_rgb(str(z8_cog), *bounds)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, f"z8 terrain took {elapsed:.1f}s"
    # 60 m × 8 = 480 m ≤ ~611 m/px < 60 m × 16: the third overview.
    assert opened[-1] == {"overview_level": 2}

    # The overview still decodes to the right elevations.
    xmin, _, xmax, ymax = bounds
    with real_open(z8_cog) as src:
        left, top = src.transform.c, src.transform.f
    px = (xmax - xmin) / terrain_tiles.TERRAIN_PX
    centres = (np.arange(terrain_tiles.TERRAIN_PX) + 0.5) * px
    cols = (xmin + centres - left) / _SRC_RES - 0.5
    rows = (top - (ymax - centres)) / _SRC_RES - 0.5
    expected = 2000 + 0.4 * cols[None, :] + 0.2 * rows[:, None]
    assert np.abs(_terrarium_m(png) - expected).max() < 1.0


@pytest.mark.parametrize("url", [
    "/tiles/terrain_rgb/7/26/48?region=colorado",
    "/tiles/terrain_rgb/3/1/3?region=colorado",
    "/tiles/contours/7/26/48?region=colorado",
    "/tiles/terrain_filter/7/26/48?region=colorado",
])
def test_below_z8_answers_204_without_rendering(url, monkeypatch, terrain_cache):
    def never(*a, **k):
        raise AssertionError("rendered below z8")

    monkeypatch.setattr(tiles, "_render_terrain_rgb", never)
    monkeypatch.setattr(tiles, "_render_contours", never)
    monkeypatch.setattr(tiles, "_render_terrain_filter", never)
    resp = TestClient(app).get(url)
    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_terrain_timeout_never_queues_a_duplicate_render(monkeypatch, terrain_cache):
    release = threading.Event()
    renders: list[str] = []
    real = _png_bytes(256, 256)

    def slow_render(dem_url, *a):
        renders.append(dem_url)
        release.wait(5)
        return real

    monkeypatch.setattr(tiles, "_TERRAIN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(tiles, "_render_terrain_rgb", slow_render)
    key = (10, 210, 394, "colorado")

    async def run():
        first = await tiles.terrain_rgb_tile(10, 210, 394, region="colorado")
        assert first.status_code == 503
        # The render is still running, so it's still in flight...
        assert key in tiles._TERRAIN_IN_FLIGHT
        # ...and a retry waits on it instead of starting another.
        second = await tiles.terrain_rgb_tile(10, 210, 394, region="colorado")
        assert second.status_code == 503
        assert len(renders) == 1

        release.set()
        fut = tiles._TERRAIN_IN_FLIGHT[key]
        await asyncio.wait_for(asyncio.shield(fut), 5)
        await asyncio.sleep(0)  # let the done callback run
        assert key not in tiles._TERRAIN_IN_FLIGHT
        # The finished render was cached although no request waited for it.
        assert terrain_cache[key] == real

        third = await tiles.terrain_rgb_tile(10, 210, 394, region="colorado")
        assert third.status_code == 200 and third.body == real
        assert len(renders) == 1

    asyncio.run(run())


def test_terrain_renders_while_the_contour_and_filter_pool_is_busy(monkeypatch, terrain_cache):
    busy = ThreadPoolExecutor(max_workers=1)
    blocked = threading.Event()
    busy.submit(blocked.wait, 5)
    monkeypatch.setattr(tiles, "_POOL", busy)
    # Queued behind the blocked job, a render would time out (503).
    monkeypatch.setattr(tiles, "_TERRAIN_TIMEOUT_S", 1.0)
    real = _png_bytes(256, 256)
    monkeypatch.setattr(tiles, "_render_terrain_rgb", lambda *a: real)
    try:
        resp = TestClient(app).get(_TERRAIN_URL)
        assert resp.status_code == 200 and resp.content == real
    finally:
        blocked.set()
        busy.shutdown(wait=True)


@pytest.mark.parametrize("render, args", [
    ("_render_contours", ("dem.tif", 0.0, 0.0, 1.0, 1.0, 12)),
    ("_render_terrain_filter", ("slope.tif", "aspect.tif", 0.0, 0.0, 1.0, 1.0, 30.0, 45.0, {"N"})),
])
def test_contours_and_filter_read_with_the_gdal_timeout(render, args, monkeypatch):
    seen: list[dict] = []

    def env(**kwargs):
        seen.append(kwargs)
        raise RuntimeError("stop before reading")

    monkeypatch.setattr(tiles.rasterio, "Env", env)
    assert getattr(tiles, render)(*args) is None  # a read failure is "no tile"
    assert seen[0]["GDAL_HTTP_TIMEOUT"] == "10"
