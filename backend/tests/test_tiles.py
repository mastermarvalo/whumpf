"""Regression guard for slope tile sizing.

The slope layer is served as 512×512 tiles for 2:1 downscale anti-aliasing, and
must NOT use TiTiler's `buffer` param: `buffer` returns tilesize+2*buffer px of
neighbouring data, which a MapLibre raster source redraws inside the tile's own
extent — duplicating a strip of the neighbour's slope at every tile seam.
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import OrderedDict
from io import BytesIO

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.routers.tiles as tiles
from app.main import app


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

    params = captured["params"]
    assert params["colormap_name"] == "hsv"
    assert params["rescale"] == "0,360"
    assert str(params["tilesize"]) == "512"
    assert "buffer" not in params
    assert params["url"].endswith("/colorado/aspect.tif")

    # Out-of-range tile coords are rejected.
    assert client.get("/tiles/aspect/1/5/0?region=colorado").status_code == 400


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
