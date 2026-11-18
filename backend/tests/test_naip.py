"""NAIP z17-18 (app/services/naip.py, app/jobs/naip_index.py,
app/routers/naip.py): the index keeps the newest file per quarter quad, a
tile shows the newest file where files overlap, each tile is made once and
shared with packs, the global upstream limit holds, and /status reports the
index. Planetary Computer and MinIO are faked; tiles are rendered from small
local GeoTIFFs."""

from __future__ import annotations

import asyncio
import fcntl
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from io import BytesIO

import httpx
import numpy as np
import pytest
import rasterio
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from app import tile_tokens
from app.config import get_settings
from app.jobs import naip_index as job
from app.main import app
from app.routers import naip as naip_router
from app.routers import status
from app.services import naip, pack_cache, rasterlayer, terrain_tiles
from app.services.pack_cache import LAYERS, PackCache, PackTileError, UpstreamBusy
from tests.weather_fakes import FakeStore, fake_pointer, reset_breakers

BERTHOUD_TILE = (18, 54047, 99433)
HREF = naip.HREF_PREFIX + "v002/co/2023/co_030cm_2023/39105/"
NOW = datetime(2026, 11, 17, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_breakers(monkeypatch):
    reset_breakers(monkeypatch, "naip", "minio", "planetary_computer")


def _feature(item_id: str, date: str, gsd: float = 0.3, href: str | None = None) -> dict:
    return {"id": item_id, "bbox": [-105.8125, 39.75, -105.75, 39.8125],
            "properties": {"datetime": f"{date}T16:00:00Z", "gsd": gsd},
            "assets": {"image": {"href": href or f"{HREF}{item_id[3:]}.tif"}}}


# ── the index ──────────────────────────────────────────────────────────────────

def test_newest_file_of_each_quarter_quad_is_kept():
    features = [
        _feature("co_m_3910510_se_13_060_20191003", "2019-10-03", 0.6),
        _feature("co_m_3910510_se_13_030_20230821_20240104", "2023-08-21"),
        _feature("co_m_3910510_se_13_060_20210825", "2021-08-25", 0.6),
        _feature("co_m_3910510_sw_13_060_20210825", "2021-08-25", 0.6),
        _feature("co_m_3910510_ne_13_030_20250101", "2025-01-01", 2.0),          # too coarse
        _feature("co_m_3910510_nw_13_030_20250101", "2025-01-01", href="https://evil.example/x.tif"),
        {"id": "not_a_naip_name", "bbox": [0, 0, 1, 1], "properties": {}, "assets": {}},
    ]
    items = job.newest_per_quarter_quad(features)
    assert [(i["id"], i["date"]) for i in items] == [
        ("co_m_3910510_se_13_030_20230821_20240104", "2023-08-21"),
        ("co_m_3910510_sw_13_060_20210825", "2021-08-25"),
    ]
    assert job.summary(items) == {"items": 2, "years": {"2021": 1, "2023": 1},
                                  "dates": ["2021-08-25", "2023-08-21"]}


def test_index_never_points_outside_planetary_computer():
    with pytest.raises(ValueError, match="outside"):
        naip.items_from_json({"items": [{"id": "x", "href": "https://evil.example/x.tif",
                                         "bbox": [0, 0, 1, 1], "date": "2023-01-01", "gsd": 0.3}]})


def test_covering_is_newest_first():
    old = naip.Item("a", "a.tif", (-106.0, 39.0, -105.0, 40.0), "2021-08-25", 0.6)
    new = naip.Item("b", "b.tif", (-105.9, 39.5, -105.5, 40.0), "2023-08-21", 0.3)
    far = naip.Item("c", "c.tif", (-104.0, 38.0, -103.5, 38.5), "2023-08-21", 0.3)
    index = naip.NaipIndex("k", "r", [old, far, new])
    assert [i.id for i in index.covering((-105.8, 39.7, -105.7, 39.8))] == ["b", "a"]
    assert index.covering((-100.0, 30.0, -99.9, 30.1)) == []


class FakeStac:
    """Planetary Computer's search: two pages for the first cell, none after."""

    def __init__(self, features: list[dict]) -> None:
        self.features = features
        self.bodies: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if body.get("token") == "page2":
            return httpx.Response(200, json={"features": self.features[1:], "links": []})
        if len(self.bodies) == 1:
            return httpx.Response(200, json={"features": self.features[:1], "links": [
                {"rel": "next", "href": str(request.url), "body": dict(body, token="page2")}]})
        return httpx.Response(200, json={"features": [], "links": []})


def _run(store, stac, **kwargs):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(stac)) as client:
            return await job.run(store, client, ["colorado"], now=kwargs.pop("now", NOW), **kwargs)
    return asyncio.run(go())


def test_job_publishes_a_revision_and_keeps_it_when_unchanged():
    features = [_feature("co_m_3910510_se_13_030_20230821_20240104", "2023-08-21"),
                _feature("co_m_3910510_sw_13_030_20230821_20240104", "2023-08-21")]
    store = FakeStore(NOW)
    stac = FakeStac(features)
    assert _run(store, stac) is True
    assert stac.bodies[0]["collections"] == ["naip"] and stac.bodies[1]["token"] == "page2"
    entry = store.json(naip.pointer_key("colorado"))["current"]
    assert entry["items"] == 2 and entry["years"] == {"2023": 2}
    assert entry["key"] == naip.index_key("colorado", entry["revision"]) and entry["key"] in store.objects
    assert naip.items_from_json(store.json(entry["key"]))[0].gsd == 0.3

    later = NOW + timedelta(days=30)
    store.puts.clear()
    assert _run(store, FakeStac(features), now=later) is True
    again = store.json(naip.pointer_key("colorado"))["current"]
    assert again["revision"] == entry["revision"] and again["valid"] == rasterlayer.iso(later)
    assert store.puts == [naip.pointer_key("colorado")]  # no new revision file


def test_job_refuses_a_much_shorter_list_unless_forced():
    many = [_feature(f"co_m_39105{i:02d}_se_13_030_20230821", "2023-08-21") for i in range(10)]
    store = FakeStore(NOW)
    assert _run(store, FakeStac(many)) is True
    served = store.json(naip.pointer_key("colorado"))["current"]
    assert _run(store, FakeStac(many[:5])) is False
    assert store.json(naip.pointer_key("colorado"))["current"] == served
    assert _run(store, FakeStac(many[:5]), force=True) is True
    assert store.json(naip.pointer_key("colorado"))["current"]["items"] == 5


# ── a tile ───────────────────────────────────────────────────────────────────

def _geotiff(path, bounds_utm, color) -> str:
    """A 3-band 0.6 m GeoTIFF in UTM 13N filled with one colour."""
    west, south, east, north = bounds_utm
    w, h = int((east - west) / 0.6), int((north - south) / 0.6)
    data = np.empty((3, h, w), dtype=np.uint8)
    for band, value in enumerate(color):
        data[band] = value
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=3, dtype="uint8",
                       crs="EPSG:26913", transform=from_origin(west, north, 0.6, 0.6)) as dst:
        dst.write(data)
    return str(path)


def _tile_utm() -> tuple[float, float, float, float]:
    return transform_bounds(terrain_tiles.WM_CRS, "EPSG:26913",
                            *terrain_tiles.tile_mercator_bounds(*BERTHOUD_TILE))


def _pixel(jpeg: bytes, row: int, col: int) -> tuple[int, int, int]:
    return Image.open(BytesIO(jpeg)).convert("RGB").getpixel((col, row))


def test_tile_shows_the_newest_file_where_they_overlap(tmp_path):
    west, south, east, north = _tile_utm()
    mid = (west + east) / 2
    older = _geotiff(tmp_path / "older.tif", (west - 50, south - 50, east + 50, north + 50), (200, 0, 0))
    newer = _geotiff(tmp_path / "newer.tif", (west - 50, south - 50, mid, north + 50), (0, 0, 200))
    items = [naip.Item("new", newer, (0, 0, 0, 0), "2023-08-21", 0.3),
             naip.Item("old", older, (0, 0, 0, 0), "2021-08-25", 0.6)]
    jpeg = naip.render_tile(items, *BERTHOUD_TILE)
    assert Image.open(BytesIO(jpeg)).size == (256, 256)
    r, _, b = _pixel(jpeg, 128, 20)
    assert b > 150 and r < 60          # west: the newer file
    r, _, b = _pixel(jpeg, 128, 235)
    assert r > 150 and b < 60          # east: only the older one reaches


def test_tile_with_no_pixels_is_empty(tmp_path):
    _, south, east, north = _tile_utm()
    away = _geotiff(tmp_path / "away.tif", (east + 500, south, east + 600, north), (200, 0, 0))
    assert naip.render_tile([naip.Item("x", away, (0, 0, 0, 0), "2023-08-21", 0.3)], *BERTHOUD_TILE) == b""


def test_a_failed_read_raises(tmp_path):
    with pytest.raises(rasterio.errors.RasterioIOError):
        naip.render_tile([naip.Item("x", str(tmp_path / "gone.tif"), (0, 0, 0, 0), "2023-08-21", 0.3)],
                         *BERTHOUD_TILE)


# ── the cache, shared by the live route and packs ─────────────────────────────

ENTRY = {"revision": "20261117-aaaaaaaa", "key": "naip/colorado/20261117-aaaaaaaa.json",
         "valid": "2026-11-17T12:00:00Z", "items": 1, "years": {"2023": 1},
         "dates": ["2023-08-21", "2023-08-21"]}


@pytest.fixture
def pointer(monkeypatch):
    return fake_pointer(monkeypatch, naip, {"current": dict(ENTRY)})


@pytest.fixture
def renders(monkeypatch):
    """naip.load_index gives one file over Berthoud; render_tile is faked
    and counted."""
    state = {"calls": [], "data": b"\xff\xd8 naip jpeg", "error": None}
    item = naip.Item("co_m_3910510_se_13_030_20230821_20240104", HREF + "m.tif",
                     (-105.8125, 39.75, -105.75, 39.8125), "2023-08-21", 0.3)

    async def fake_index(region):
        return naip.NaipIndex(ENTRY["key"], ENTRY["revision"], [item])

    def fake_render(items, z, x, y):
        state["calls"].append((z, x, y, [i.id for i in items]))
        if state["error"]:
            raise state["error"]
        return state["data"]

    monkeypatch.setattr(naip, "load_index", fake_index)
    monkeypatch.setattr(naip, "render_tile", fake_render)
    return state


def test_naip_tile_is_made_once_and_versioned_by_the_index(tmp_path, pointer, renders):
    cache = PackCache(tmp_path)

    async def go():
        a = await cache.get_tile("naip", "colorado", *BERTHOUD_TILE)
        b = await cache.get_tile("naip", "colorado", *BERTHOUD_TILE)
        return a, b

    assert asyncio.run(go()) == (renders["data"], renders["data"])
    assert len(renders["calls"]) == 1
    assert (tmp_path / "naip_colorado.sqlite").exists()
    assert cache._store(LAYERS["naip"], "colorado").get(*BERTHOUD_TILE)[2] == ENTRY["revision"]

    pointer["value"]["current"]["revision"] = "20271117-bbbbbbbb"  # a new NAIP year
    asyncio.run(cache.get_tile("naip", "colorado", *BERTHOUD_TILE))
    assert len(renders["calls"]) == 2


def test_naip_tile_outside_every_file_is_empty_without_a_read(tmp_path, pointer, renders):
    cache = PackCache(tmp_path)
    assert asyncio.run(cache.get_tile("naip", "colorado", 18, 54500, 99000)) == b""
    assert renders["calls"] == []


def test_naip_needs_an_index(tmp_path, pointer, renders):
    pointer["value"] = {}
    with pytest.raises(PackTileError, match="make naip-index"):
        asyncio.run(PackCache(tmp_path).get_tile("naip", "colorado", *BERTHOUD_TILE))


def test_naip_read_failure_is_never_stored(tmp_path, pointer, renders):
    renders["error"] = rasterio.errors.RasterioIOError("HTTP 503")
    cache = PackCache(tmp_path)
    with pytest.raises(PackTileError, match="NAIP"):
        asyncio.run(cache.get_tile("naip", "colorado", *BERTHOUD_TILE))
    assert cache._store(LAYERS["naip"], "colorado").get(*BERTHOUD_TILE) is None


def _busy_once(monkeypatch) -> list[str]:
    """The first naip_upstream hit is over the limit; every other bucket passes."""
    hits: list[str] = []

    def fake_enforce(bucket, subject):
        if bucket != "naip_upstream":
            return
        hits.append(subject)
        if len(hits) == 1:
            raise HTTPException(429, headers={"Retry-After": "7"})

    monkeypatch.setattr(tile_tokens, "enforce", fake_enforce)
    return hits


def test_pack_worker_waits_out_the_upstream_limit(tmp_path, pointer, renders, monkeypatch):
    hits = _busy_once(monkeypatch)
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(pack_cache.asyncio, "sleep", fake_sleep)
    data = asyncio.run(PackCache(tmp_path).get_tile("naip", "colorado", *BERTHOUD_TILE))
    assert data == renders["data"] and hits == ["global", "global"] and slept == [7]


def test_api_gives_up_at_once_at_the_upstream_limit(tmp_path, pointer, renders, monkeypatch):
    _busy_once(monkeypatch)
    with pytest.raises(UpstreamBusy) as exc:
        asyncio.run(PackCache(tmp_path, wait_when_busy=False).get_tile("naip", "colorado", *BERTHOUD_TILE))
    assert exc.value.retry_after == 7 and renders["calls"] == []


def test_upstream_limit_is_global():
    assert [str(i) for i in tile_tokens.BUCKETS["naip_upstream"]] == ["300 per 1 minute", "15000 per 1 hour"]


def test_eviction_waits_for_another_process(tmp_path):
    cache = PackCache(tmp_path, max_bytes=0)
    with (tmp_path / ".evict.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert cache.evict() == 0


# ── /tiles/naip ──────────────────────────────────────────────────────────────

TILE = "/tiles/naip/18/54047/99433?region=colorado"
META = "/tiles/naip/meta?region=colorado"


@pytest.fixture
def live(tmp_path, monkeypatch, pointer, renders):
    cache = PackCache(tmp_path, wait_when_busy=False)
    monkeypatch.setattr(naip_router, "_cache", cache)
    return TestClient(app)


def test_meta(live, pointer):
    body = live.get(META).json()
    assert body["available"] is True and body["minzoom"] == 17 and body["maxzoom"] == 18
    assert body["years"] == {"2023": 1} and body["dates"] == ["2023-08-21", "2023-08-21"]
    assert body["attribution"] == "USDA Farm Service Agency: NAIP imagery"
    assert body["tiles"] == f"/tiles/naip/{{z}}/{{x}}/{{y}}?region=colorado&v={ENTRY['revision']}"
    assert isinstance(body["stale"], bool)

    pointer["value"] = {}
    body = live.get(META).json()
    assert body["available"] is False and "tiles" not in body


def test_tile_is_cached_a_week_when_versioned(live, renders):
    r = live.get(f"{TILE}&v={ENTRY['revision']}")
    assert r.status_code == 200 and r.content == renders["data"]
    assert r.headers["content-type"] == "image/jpeg" and r.headers["cache-control"] == "public, max-age=604800"
    r = live.get(TILE)
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert len(renders["calls"]) == 1  # the second came from the cache


@pytest.mark.parametrize("url, code", [
    ("/tiles/naip/16/27023/49716?region=colorado", 404),
    ("/tiles/naip/19/0/0?region=colorado", 404),
    ("/tiles/naip/18/999999/0?region=colorado", 404),
    ("/tiles/naip/18/54047/99433?region=atlantis", 400),
])
def test_bad_tile_requests(live, url, code, renders):
    assert live.get(url).status_code == code and renders["calls"] == []


def test_tile_outside_the_region_is_204_without_a_read(live, renders, tmp_path):
    r = live.get("/tiles/naip/18/0/0?region=colorado")
    assert r.status_code == 204 and renders["calls"] == []
    assert not (tmp_path / "naip_colorado.sqlite").exists() or \
        sqlite_rows(tmp_path / "naip_colorado.sqlite") == 0


def sqlite_rows(path) -> int:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    finally:
        con.close()


def test_tile_with_no_imagery_is_204(live, renders):
    renders["data"] = b""
    assert live.get(TILE).status_code == 204


def test_failures_are_503_no_store(live, renders, pointer, monkeypatch):
    renders["error"] = rasterio.errors.RasterioIOError("HTTP 503")
    r = live.get(TILE)
    assert r.status_code == 503 and r.headers["cache-control"] == "no-store"

    _busy_once(monkeypatch)
    renders["error"] = None
    r = live.get(TILE)
    assert r.status_code == 503 and r.headers["retry-after"] == "7"

    pointer["value"] = httpx.ConnectError("minio down")
    assert live.get(TILE).status_code == 503

    pointer["value"] = {}
    assert live.get(TILE).status_code == 503


def test_naip_needs_a_tile_token_when_tokenless_is_off(live, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    for url in (TILE, META):
        assert live.get(url).status_code == 401
    token = tile_tokens.issue("i:install-0123456789abcdef", tile_tokens.TIER_ANON)[0]
    assert live.get(f"{TILE}&t={token}").status_code == 200
    assert live.get(f"{META}&t={token}").status_code == 200


# ── /status ──────────────────────────────────────────────────────────────────

def test_status_naip_not_set_up_fresh_stale(pointer):
    real = pointer["value"]
    pointer["value"] = {}  # make naip-index has never run: not a problem
    health = asyncio.run(status._check_naip())
    assert (health.name, health.healthy, health.detail) == ("naip", True, "not set up yet")
    pointer["value"] = httpx.ConnectError("minio down")  # and unreadable before it ever worked
    assert asyncio.run(status._check_naip()).healthy is True

    pointer["value"] = real
    real["current"]["valid"] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    health = asyncio.run(status._check_naip())
    assert (health.name, health.healthy, health.detail) == ("naip", True, None)

    real["current"]["valid"] = (datetime.now(UTC) - timedelta(days=50)).isoformat()
    assert asyncio.run(status._check_naip()).healthy is False

    pointer["value"] = httpx.ConnectError("minio down")  # it worked, now it can't be read
    assert asyncio.run(status._check_naip()).healthy is False
