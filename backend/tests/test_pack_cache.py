"""Offline pack tile cache (app/services/pack_cache.py): each tile is made or
fetched once, failures are never stored, old versions are remade, pack
tiles decode to the live endpoints' values, the concurrency limits hold and
eviction removes the least recently used tiles first."""

from __future__ import annotations

import asyncio
import itertools
import os
import sqlite3
import threading
import time
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_bounds

from app import http_retry
from app.routers import tiles
from app.services import pack_cache, terrain_tiles
from app.services.pack_cache import LAYERS, PackCache, PackTileError, tiles_in_bbox

_DATA = Path(__file__).parent / "data"
_JPEG = b"\xff\xd8\xff\xe0 a jpeg"
_TOPO = (12, 844, 1553)
_DAY = 86400


@pytest.fixture(autouse=True)
def fresh_breakers(monkeypatch):
    for name in ("usgs", "titiler", "minio"):
        monkeypatch.delitem(http_retry._BREAKERS, name, raising=False)
    yield
    for name in ("usgs", "titiler", "minio"):
        http_retry._BREAKERS.pop(name, None)


class Upstream:
    """A fake USGS + TiTiler + MinIO behind httpx.MockTransport. `respond`
    answers GETs; HEADs answer the ETag in `etags` (default "v1")."""

    def __init__(self, respond=None) -> None:
        self.respond = respond or (lambda request: httpx.Response(
            200, content=_JPEG, headers={"content-type": "image/jpeg"}))
        self.gets: list[httpx.Request] = []
        self.heads: list[httpx.Request] = []
        self.etags: dict[str, str] = {}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            self.heads.append(request)
            key = request.url.path.split("/", 2)[2]  # strip /<bucket>/
            return httpx.Response(200, headers={"etag": f'"{self.etags.get(key, "v1")}"'})
        self.gets.append(request)
        response = self.respond(request)
        return await response if asyncio.iscoroutine(response) else response


class Clock:
    def __init__(self, now: float = 1_800_000_000) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def make_cache(tmp_path):
    def _make(upstream: Upstream, **kwargs) -> PackCache:
        kwargs.setdefault("usgs_contact", "test@example.com")
        kwargs.setdefault("ski_dir", tmp_path / "ski")
        return PackCache(
            tmp_path / "cache", http=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
            **kwargs,
        )
    return _make


def _stored(cache: PackCache, layer: str, z: int, x: int, y: int, region: str = "colorado"):
    return cache._store(LAYERS[layer], region).get(z, x, y)


# ── USGS ───────────────────────────────────────────────────────────────────────

def test_usgs_tile_fetched_once(make_cache):
    upstream = Upstream()
    cache = make_cache(upstream)

    async def run():
        first = await cache.get_tile("topo", "colorado", *_TOPO)
        second = await cache.get_tile("topo", "colorado", *_TOPO)
        return first, second

    assert asyncio.run(run()) == (_JPEG, _JPEG)
    assert len(upstream.gets) == 1
    request = upstream.gets[0]
    assert request.url.path == "/arcgis/rest/services/USGSTopo/MapServer/tile/12/1553/844"  # z/y/x
    assert request.headers["user-agent"] == "whumpf/0.1 (+https://whumpf.co; test@example.com)"
    assert (cache.dir / "topo.sqlite").exists()  # USGS files aren't per region


def test_imagery_uses_naip_service(make_cache):
    upstream = Upstream()
    cache = make_cache(upstream)
    asyncio.run(cache.get_tile("imagery", "colorado", *_TOPO))
    assert "/USGSImageryOnly/MapServer/tile/" in upstream.gets[0].url.path


@pytest.mark.parametrize("bad", [
    httpx.Response(500),
    httpx.Response(429, headers={"retry-after": "0"}),
    httpx.Response(200, content=b"<html>error</html>", headers={"content-type": "text/html"}),
    httpx.Response(200, content=b"", headers={"content-type": "image/jpeg"}),
], ids=["500", "429", "html", "empty-body"])
def test_failed_usgs_answer_not_stored(make_cache, bad):
    answers = [bad]
    upstream = Upstream(lambda request: answers.pop(0) if answers else httpx.Response(
        200, content=_JPEG, headers={"content-type": "image/jpeg"}))
    cache = make_cache(upstream)
    cache.usgs_attempts = 1

    async def run():
        with pytest.raises(PackTileError):
            await cache.get_tile("topo", "colorado", *_TOPO)
        assert _stored(cache, "topo", *_TOPO) is None
        return await cache.get_tile("topo", "colorado", *_TOPO)

    assert asyncio.run(run()) == _JPEG
    assert len(upstream.gets) == 2


def test_usgs_429_is_retried(make_cache):
    answers = [httpx.Response(429, headers={"retry-after": "0"})]
    upstream = Upstream(lambda request: answers.pop(0) if answers else httpx.Response(
        200, content=_JPEG, headers={"content-type": "image/jpeg"}))
    cache = make_cache(upstream)
    cache.usgs_attempts = 2
    assert asyncio.run(cache.get_tile("topo", "colorado", *_TOPO)) == _JPEG
    assert len(upstream.gets) == 2


def test_usgs_404_stored_empty_and_asked_again_after_30_days(make_cache):
    upstream = Upstream(lambda request: httpx.Response(404, text="not found"))
    clock = Clock()
    cache = make_cache(upstream, clock=clock)

    async def run():
        assert await cache.get_tile("topo", "colorado", *_TOPO) == b""
        clock.now += 29 * _DAY
        assert await cache.get_tile("topo", "colorado", *_TOPO) == b""
        assert len(upstream.gets) == 1
        clock.now += 1 * _DAY
        assert await cache.get_tile("topo", "colorado", *_TOPO) == b""
        assert len(upstream.gets) == 2

    asyncio.run(run())


def test_real_usgs_tiles_never_expire(make_cache):
    upstream = Upstream()
    clock = Clock()
    cache = make_cache(upstream, clock=clock)

    async def run():
        await cache.get_tile("topo", "colorado", *_TOPO)
        clock.now += 3 * 365 * _DAY
        await cache.get_tile("topo", "colorado", *_TOPO)

    asyncio.run(run())
    assert len(upstream.gets) == 1


def test_usgs_refused_without_contact(make_cache):
    upstream = Upstream()
    cache = make_cache(upstream, usgs_contact="")
    with pytest.raises(PackTileError, match="USGS_CONTACT"):
        asyncio.run(cache.get_tile("topo", "colorado", *_TOPO))
    assert upstream.gets == []


def test_at_most_4_usgs_requests_at_a_time(make_cache):
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    upstream = Upstream(respond)
    cache = make_cache(upstream)

    async def run():
        await asyncio.gather(*(
            cache.get_tile(layer, "colorado", 14, 3376 + i, 6212)
            for layer in ("topo", "imagery") for i in range(8)
        ))

    asyncio.run(run())
    assert len(upstream.gets) == 16
    assert peak == 4


def test_same_tile_at_once_is_fetched_once(make_cache):
    async def respond(request):
        await asyncio.sleep(0.02)
        return httpx.Response(200, content=_JPEG, headers={"content-type": "image/jpeg"})

    upstream = Upstream(respond)
    cache = make_cache(upstream)

    async def run():
        return await asyncio.gather(*(cache.get_tile("topo", "colorado", *_TOPO) for _ in range(5)))

    assert asyncio.run(run()) == [_JPEG] * 5
    assert len(upstream.gets) == 1


# ── slope and aspect: TiTiler, palette PNGs ──────────────────────────────────

def _colormapped_png(colormap_file: str, entries: int, nodata_rgb) -> tuple[bytes, np.ndarray]:
    colormap = np.load(_DATA / colormap_file)[:entries]
    values = np.random.default_rng(0).permutation(512 * 512) % len(colormap)
    rgba = colormap[values].reshape(512, 512, 4).copy()
    rgba[:16] = (*nodata_rgb, 0)
    buf = BytesIO()
    Image.fromarray(rgba).save(buf, "PNG")
    return buf.getvalue(), rgba


@pytest.mark.parametrize("layer, colormap_file, entries, nodata_rgb", [
    ("slope", "caltopo_slope.npy", 256, (0, 0, 0)),
    ("aspect", "titiler_hsv.npy", 255, (255, 0, 0)),
])
def test_palette_tile_is_titilers_pixels_from_the_live_request(
    make_cache, monkeypatch, layer, colormap_file, entries, nodata_rgb,
):
    titiler_png, rgba = _colormapped_png(colormap_file, entries, nodata_rgb)
    upstream = Upstream(lambda request: httpx.Response(
        200, content=titiler_png, headers={"content-type": "image/png"}))
    cache = make_cache(upstream)
    z, x, y = 12, 844, 1553

    async def run():
        first = await cache.get_tile(layer, "colorado", z, x, y)
        assert await cache.get_tile(layer, "colorado", z, x, y) == first
        return first

    out = asyncio.run(run())
    assert len(upstream.gets) == 1  # made once
    assert (cache.dir / f"{layer}_colorado.sqlite").exists()

    img = Image.open(BytesIO(out))
    assert img.mode == "P"
    expected = rgba.copy()
    expected[expected[..., 3] == 0] = 0  # invisible either way
    assert np.array_equal(np.asarray(img.convert("RGBA")), expected)

    # The pack asked TiTiler exactly what the live endpoint asks (no hires).
    live: dict = {}

    async def fake_get(url, params=None, **kwargs):
        live["url"] = httpx.URL(url, params=params)
        return httpx.Response(200, content=titiler_png, request=httpx.Request("GET", url))

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)
    monkeypatch.setattr(tiles, "_SLOPE_CACHE", type(tiles._SLOPE_CACHE)())
    monkeypatch.setattr(tiles, "_ASPECT_CACHE", type(tiles._ASPECT_CACHE)())
    handler = tiles.slope_tile if layer == "slope" else tiles.aspect_tile
    resp = asyncio.run(handler(z, x, y, region="colorado"))
    assert live["url"] == upstream.gets[0].url
    assert resp.body == out


def test_titiler_404_stored_empty(make_cache):
    upstream = Upstream(lambda request: httpx.Response(404))
    cache = make_cache(upstream)

    async def run():
        assert await cache.get_tile("slope", "colorado", 8, 52, 97) == b""
        assert await cache.get_tile("slope", "colorado", 8, 52, 97) == b""

    asyncio.run(run())
    assert len(upstream.gets) == 1


def test_new_cog_version_is_remade(make_cache):
    titiler_png, _ = _colormapped_png("caltopo_slope.npy", 256, (0, 0, 0))
    upstream = Upstream(lambda request: httpx.Response(
        200, content=titiler_png, headers={"content-type": "image/png"}))
    cache = make_cache(upstream)
    cache.versions.ttl_s = 0

    async def run():
        await cache.get_tile("slope", "colorado", 10, 211, 388)
        await cache.get_tile("slope", "colorado", 10, 211, 388)
        assert len(upstream.gets) == 1
        upstream.etags["colorado/slope.tif"] = "v2"
        await cache.get_tile("slope", "colorado", 10, 211, 388)
        assert len(upstream.gets) == 2

    asyncio.run(run())
    assert _stored(cache, "slope", 10, 211, 388)[2] == "v2"


def test_elevation_version_names_the_render(make_cache):
    # A change to the terrain render remakes elevation tiles like a new COG.
    upstream = Upstream(lambda request: httpx.Response(404))
    upstream.etags.update({"colorado/dem.tif": "d1", "colorado/dem_hires.tif": "h1"})
    cache = make_cache(upstream)
    render = terrain_tiles.ELEVATION_RENDER

    async def run():
        spec = LAYERS["elevation"]
        assert await cache.versions.version(spec, "colorado", 12) == f"d1|{render}"
        assert await cache.versions.version(spec, "colorado", 13) == f"h1|d1|{render}"
        assert await cache.versions.version(LAYERS["slope"], "colorado", 12) == "v1"

    asyncio.run(run())


# ── elevation ──────────────────────────────────────────────────────────────────

def _terrarium_m(png: bytes) -> np.ndarray:
    px = np.asarray(Image.open(BytesIO(png)).convert("RGB"), dtype=np.float64)
    return px[..., 0] * 256 + px[..., 1] + px[..., 2] / 256 - 32768


@pytest.fixture
def synthetic_dem(tmp_path, monkeypatch) -> Path:
    """A 3857 GeoTIFF over tile 12/844/1553 with a margin, fractional metres,
    served as both DEM urls' 10m file; the hires file doesn't exist."""
    xmin, ymin, xmax, ymax = terrain_tiles.tile_mercator_bounds(12, 844, 1553)
    pad = (xmax - xmin) * 0.1
    size = 400
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:size, 0:size]
    elev = (2500 + 3 * xx + 2 * yy + rng.random((size, size)) * 40).astype(np.float32)
    elev[:20, :20] = -9999  # a little nodata
    path = tmp_path / "dem.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=size, height=size, count=1, dtype="float32",
        crs="EPSG:3857", nodata=-9999,
        transform=from_bounds(xmin - pad, ymin - pad, xmax + pad, ymax + pad, size, size),
    ) as dst:
        dst.write(elev, 1)

    def url(settings, region, hires=False):
        return str(tmp_path / "dem_hires.tif") if hires else str(path)

    monkeypatch.setattr(tiles, "_build_dem_url", url)
    monkeypatch.setattr(terrain_tiles, "build_dem_url", url)
    monkeypatch.setattr(tiles, "_TERRAIN_CACHE", type(tiles._TERRAIN_CACHE)())
    monkeypatch.setattr(tiles, "_TERRAIN_IN_FLIGHT", {})
    return path


@pytest.mark.parametrize("zxy", [(12, 844, 1553), (13, 1689, 3107)], ids=["10m", "hires-fallback"])
def test_elevation_tile_decodes_to_live_values(make_cache, synthetic_dem, zxy):
    upstream = Upstream()
    cache = make_cache(upstream)

    async def run():
        live = await tiles.terrain_rgb_tile(*zxy, region="colorado")
        pack = await cache.get_tile("elevation", "colorado", *zxy)
        return live, pack

    live, pack = asyncio.run(run())
    assert live.status_code == 200
    assert live.body != terrain_tiles.FLAT_TERRAIN_PNG
    assert np.array_equal(_terrarium_m(pack), _terrarium_m(live.body))
    assert len(pack) <= len(live.body)  # level 9 vs the live level 1
    keys = [h.url.path for h in upstream.heads]
    expected = ["/dem-cogs/colorado/dem.tif"]
    if zxy[0] >= 13:
        expected = ["/dem-cogs/colorado/dem_hires.tif", *expected]
    assert keys == expected


def test_elevation_without_data_is_the_live_flat_tile(make_cache, monkeypatch):
    monkeypatch.setattr(terrain_tiles, "render_terrain_rgb_with_fallback", lambda *a: None)
    cache = make_cache(Upstream())
    assert asyncio.run(cache.get_tile("elevation", "colorado", 10, 211, 388)) == tiles._FLAT_TERRAIN_PNG


def test_elevation_read_failure_not_stored(make_cache, monkeypatch):
    def boom(*a):
        raise RuntimeError("vsicurl read failed")

    monkeypatch.setattr(terrain_tiles, "render_terrain_rgb_with_fallback", boom)
    cache = make_cache(Upstream())
    with pytest.raises(PackTileError):
        asyncio.run(cache.get_tile("elevation", "colorado", 10, 211, 388))
    assert _stored(cache, "elevation", 10, 211, 388) is None


def test_at_most_2_of_our_tiles_at_a_time(make_cache, monkeypatch):
    lock = threading.Lock()
    active = peak = 0
    calls = 0

    def render(*a):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return b"png"

    monkeypatch.setattr(terrain_tiles, "render_terrain_rgb_with_fallback", render)
    cache = make_cache(Upstream())

    async def run():
        await asyncio.gather(*(cache.get_tile("elevation", "colorado", 12, 844 + i, 1553) for i in range(8)))
        await asyncio.gather(*(cache.get_tile("elevation", "colorado", 12, 844 + i, 1553) for i in range(8)))

    asyncio.run(run())
    assert calls == 8  # the second round came from the cache
    assert peak == 2


# ── ski ────────────────────────────────────────────────────────────────────────

def _write_mbtiles(path: Path, tiles_: dict[tuple[int, int, int], bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB)")
    for (z, x, y), data in tiles_.items():
        con.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)", (z, x, (1 << z) - 1 - y, data))
    con.commit()
    con.close()


def test_ski_tile_read_as_stored_and_remade_on_a_new_file(make_cache, tmp_path):
    mbtiles = tmp_path / "ski" / "ski_colorado.mbtiles"
    _write_mbtiles(mbtiles, {(12, 844, 1553): b"\x1f\x8b gzipped mvt"})
    cache = make_cache(Upstream())

    async def run():
        assert await cache.get_tile("ski", "colorado", 12, 844, 1553) == b"\x1f\x8b gzipped mvt"
        assert await cache.get_tile("ski", "colorado", 12, 845, 1553) == b""  # no runs there

        # Same file: served from the cache, even if the file now differs.
        stat = mbtiles.stat()
        con = sqlite3.connect(mbtiles)
        con.execute("UPDATE tiles SET tile_data = ?", (b"\x1f\x8b rebuilt",))
        con.commit()
        con.close()
        os.utime(mbtiles, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # keep the old mtime
        assert await cache.get_tile("ski", "colorado", 12, 844, 1553) == b"\x1f\x8b gzipped mvt"

        # A new build (new mtime) is read again.
        os.utime(mbtiles, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        assert await cache.get_tile("ski", "colorado", 12, 844, 1553) == b"\x1f\x8b rebuilt"

    asyncio.run(run())


def test_ski_missing_file_is_an_error(make_cache):
    with pytest.raises(PackTileError, match="ski_colorado.mbtiles"):
        asyncio.run(make_cache(Upstream()).get_tile("ski", "colorado", 12, 844, 1553))


def test_out_of_range_tiles_rejected(make_cache):
    cache = make_cache(Upstream())
    with pytest.raises(ValueError):
        asyncio.run(cache.get_tile("slope", "colorado", 15, 0, 0))  # slope stops at z14
    with pytest.raises(ValueError):
        asyncio.run(cache.get_tile("topo", "colorado", 8, 256, 0))
    with pytest.raises(ValueError):
        asyncio.run(cache.get_tile("hillshade", "colorado", 8, 0, 0))


# ── eviction ───────────────────────────────────────────────────────────────────

def test_eviction_removes_least_recently_used_first(make_cache):
    upstream = Upstream()
    clock = Clock(1000)
    cache = make_cache(upstream, clock=clock)
    topo = cache._store(LAYERS["topo"], "colorado")
    slope = cache._store(LAYERS["slope"], "colorado")
    # Alternate layers so the order runs across both files.
    for t in range(1, 9):
        store = topo if t % 2 else slope
        store.put(10, t, 0, os.urandom(50_000), None, now=t)

    # A hit on the oldest tile makes it the most recently used.
    clock.now = 100
    assert len(asyncio.run(cache.get_tile("topo", "colorado", 10, 1, 0))) == 50_000
    assert upstream.gets == []

    def kept() -> list[int]:
        return sorted(
            row[0] for store in (topo, slope)
            for row in store.db.execute("SELECT last_used_at FROM tiles"))

    disk_before = pack_cache.disk_usage(cache.dir)
    cache.max_bytes = cache.used_bytes() - 160_000  # three tiles over
    removed = cache.evict()

    # What's left is the newest few, with the touched tile among them.
    left = kept()
    assert left == [2, 3, 4, 5, 6, 7, 8, 100][-len(left):]
    assert len(left) <= 5
    assert removed == 8 - len(left)
    assert cache.used_bytes() <= cache.max_bytes * pack_cache.EVICT_TO
    assert pack_cache.disk_usage(cache.dir) < disk_before  # pages given back

    # Under the cap: nothing more goes.
    assert cache.evict() == 0


# ── tile listing ───────────────────────────────────────────────────────────────

def test_tiles_in_bbox():
    # ~25 x 25 km around Berthoud Pass.
    box = (-105.93, 39.69, -105.64, 39.91)
    by_zoom: dict[int, int] = {}
    for z, _, _ in tiles_in_bbox(box, 8, 16):
        by_zoom[z] = by_zoom.get(z, 0) + 1
    assert set(by_zoom) == set(range(8, 17))
    assert 2500 <= by_zoom[16] <= 3500
    assert by_zoom[8] <= 2

    # A point gives one tile per zoom, each inside its parent.
    point = list(tiles_in_bbox((-105.78, 39.80, -105.78, 39.80), 8, 14))
    assert [z for z, _, _ in point] == list(range(8, 15))
    for (z, x, y), (_, cx, cy) in itertools.pairwise(point):
        assert (cx >> 1, cy >> 1) == (x, y)
