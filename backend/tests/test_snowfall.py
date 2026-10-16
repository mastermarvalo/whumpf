"""Recent snowfall: the hourly NOHRSC ingest, /tiles/snowfall/*, and its
/status freshness check. NOHRSC, MinIO and TiTiler are all faked."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.jobs import snowfall_refresh as job
from app.main import app
from app.regions import REGIONS
from app.routers import status, tiles
from app.services import snowfall

CO = REGIONS["colorado"]
NOW = datetime(2026, 10, 15, 14, 25, tzinfo=UTC)  # after the 12Z issue
BASE = "https://nohrsc.test/snowfall/data"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    for name in ("nohrsc", "titiler", "minio"):
        monkeypatch.delitem(http_retry._BREAKERS, name, raising=False)
    monkeypatch.setattr(snowfall, "_pointers", {})
    monkeypatch.setattr(tiles, "_SNOWFALL_CACHE", OrderedDict())
    yield
    for name in ("nohrsc", "titiler", "minio"):
        http_retry._BREAKERS.pop(name, None)


# ── fakes ─────────────────────────────────────────────────────────────────────

def nohrsc_tif(
    peak: float = 12.4, base: float = 2.0, *, nodata_tag: bool = True,
    res: float = 0.04, crs: str = "EPSG:4326",
) -> bytes:
    """A NOHRSC-like grid over -112..-100, 34..44 (a CONUS crop): `base`
    inches everywhere, `peak` near Tenmile, -99999 in a corner inside the
    Colorado window."""
    w, h = int(12 / res), int(10 / res)
    data = np.full((h, w), base, dtype=np.float32)
    transform = from_origin(-112, 44, res, res)
    row, col = rasterio.transform.rowcol(transform, -106.18, 39.08)
    data[row, col] = peak
    r0, c0 = rasterio.transform.rowcol(transform, -109.4, 41.4)
    data[r0:r0 + 5, c0:c0 + 5] = -99999
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=crs,
            transform=transform, nodata=-99999 if nodata_tag else None,
        ) as dst:
            dst.write(data, 1)
        return mem.read()


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.puts: list[str] = []
        self.now = NOW

    def get(self, key):
        return self.objects[key][0] if key in self.objects else None

    def put(self, key, body, content_type):
        self.objects[key] = (body, self.now)
        self.puts.append(key)

    def list(self, prefix):
        return [(k, m) for k, (_, m) in self.objects.items() if k.startswith(prefix)]

    def delete(self, keys):
        for k in keys:
            self.objects.pop(k)

    def pointer(self) -> dict:
        return json.loads(self.get(snowfall.pointer_key("colorado")))


class FakeNohrsc:
    """files[url] = (body, etag). Everything else is 404. Matching
    If-None-Match gets 304."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.requests: list[tuple[str, str | None]] = []

    def add(self, period: str, valid: datetime, body: bytes, etag: str) -> None:
        self.files[job.file_url(BASE, period, valid)] = (body, etag)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        inm = request.headers.get("if-none-match")
        self.requests.append((url, inm))
        if url not in self.files:
            return httpx.Response(404)
        body, etag = self.files[url]
        if inm == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=body, headers={"etag": etag, "last-modified": "x"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def run_job(store: FakeStore, noaa: FakeNohrsc, now: datetime = NOW) -> bool:
    async def go():
        async with noaa.client() as client:
            return await job.run(store, client, ["colorado"], now=now, base=BASE)
    return asyncio.run(go())


ISSUE_12Z = datetime(2026, 10, 15, 12, tzinfo=UTC)


@pytest.fixture
def noaa() -> FakeNohrsc:
    n = FakeNohrsc()
    n.add("24h", ISSUE_12Z, nohrsc_tif(), '"a"')
    n.add("72h", ISSUE_12Z, nohrsc_tif(peak=27.2, nodata_tag=False), '"b"')
    return n


# ── clip_to_cog ───────────────────────────────────────────────────────────────

def test_clip_makes_a_region_cog_in_inches_with_nodata():
    # The 72 h files have -99999 but no NoData tag.
    cog, stats = job.clip_to_cog(nohrsc_tif(peak=27.2, nodata_tag=False), CO.max_bounds)
    assert stats["max_in"] == 27.2
    with MemoryFile(cog) as mem, mem.open() as src:
        assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert src.crs.to_epsg() == 4326
        assert src.nodata == -99999
        west, south, east, north = src.bounds
        assert west <= CO.max_bounds[0] and south <= CO.max_bounds[1]
        assert east >= CO.max_bounds[2] and north >= CO.max_bounds[3]
        assert east - west < 8.2 and north - south < 5.2  # clipped, not CONUS
        data = src.read(1)
        assert float(data.max()) == pytest.approx(27.2)  # inches, untouched
        assert (data == -99999).sum() == 25
        assert float(data[data != -99999].min()) == pytest.approx(2.0)


@pytest.mark.parametrize(
    "body, why",
    [
        (nohrsc_tif(res=0.05), "pixels"),
        (nohrsc_tif(crs="EPSG:4269"), "EPSG:4326"),
        (nohrsc_tif(peak=500), "out of range"),
        (nohrsc_tif(peak=-99999, base=-99999), "has data"),
        (b"<html>Service Unavailable</html>", "readable"),
    ],
)
def test_clip_rejects_files_that_are_not_the_grid(body, why):
    with pytest.raises(job.BadFile, match=why):
        job.clip_to_cog(body, CO.max_bounds)


def test_issue_times_are_00z_and_12z_newest_first():
    times = job.issue_times(NOW)
    assert times[:3] == [ISSUE_12Z, ISSUE_12Z - timedelta(hours=12), ISSUE_12Z - timedelta(hours=24)]
    assert all(t.hour in (0, 12) for t in times)
    assert job.file_url(BASE, "72h", ISSUE_12Z) == f"{BASE}/202610/sfav2_CONUS_72h_2026101512.tif"


# ── run: publish, revisions, fallbacks ────────────────────────────────────────

def test_run_publishes_newest_issue_per_period(noaa):
    store = FakeStore()
    assert run_job(store, noaa) is True

    p = store.pointer()
    e24, e72 = p["periods"]["24h"], p["periods"]["72h"]
    assert e24["valid"] == "2026-10-15T12:00:00Z" and e24["start"] == "2026-10-14T12:00:00Z"
    assert e72["valid"] == "2026-10-15T12:00:00Z" and e72["start"] == "2026-10-12T12:00:00Z"
    assert e24["max_in"] == 12.4 and e72["max_in"] == 27.2
    assert e24["revision"].startswith("2026101512-")
    assert e24["key"] == f"snowfall/colorado/24h/{e24['revision']}.tif"
    with MemoryFile(store.get(e24["key"])) as mem, mem.open() as src:
        assert src.nodata == -99999
    # Newest first: at 14:25Z the 12Z issue is the newest candidate.
    assert noaa.requests[0][0].endswith("_24h_2026101512.tif")


def test_unchanged_file_is_a_conditional_get_and_no_write(noaa):
    store = FakeStore()
    run_job(store, noaa)
    before = store.pointer()
    store.puts.clear()
    noaa.requests.clear()

    assert run_job(store, noaa, NOW + timedelta(hours=1)) is True
    assert store.puts == []
    assert store.pointer() == before
    assert (job.file_url(BASE, "24h", ISSUE_12Z), '"a"') in noaa.requests


def test_revised_file_gets_a_new_revision_and_key(noaa):
    store = FakeStore()
    run_job(store, noaa)
    old = store.pointer()["periods"]["24h"]

    noaa.add("24h", ISSUE_12Z, nohrsc_tif(peak=14.0), '"a2"')  # late reports
    assert run_job(store, noaa, NOW + timedelta(hours=1)) is True
    new = store.pointer()["periods"]["24h"]
    assert new["valid"] == old["valid"]
    assert new["revision"] != old["revision"] and new["key"] != old["key"]
    assert new["max_in"] == 14.0
    assert store.get(old["key"]) is not None  # still there for in-flight tiles


def test_same_bytes_with_new_etag_only_updates_the_etag(noaa):
    store = FakeStore()
    run_job(store, noaa)
    old = store.pointer()["periods"]["24h"]
    store.puts.clear()

    noaa.add("24h", ISSUE_12Z, nohrsc_tif(), '"a3"')
    run_job(store, noaa, NOW + timedelta(hours=1))
    new = store.pointer()["periods"]["24h"]
    assert new == {**old, "etag": '"a3"'}
    assert store.puts == [snowfall.pointer_key("colorado")]


def test_newer_issue_replaces_the_served_one(noaa):
    store = FakeStore()
    run_job(store, noaa)
    later = datetime(2026, 10, 16, 1, 25, tzinfo=UTC)
    noaa.add("24h", datetime(2026, 10, 16, 0, tzinfo=UTC), nohrsc_tif(peak=3.0), '"c"')

    assert run_job(store, noaa, later) is True
    p = store.pointer()
    assert p["periods"]["24h"]["valid"] == "2026-10-16T00:00:00Z"
    assert p["periods"]["72h"]["valid"] == "2026-10-15T12:00:00Z"  # no 00Z yet


def test_bad_newer_file_never_replaces_good_data(noaa):
    store = FakeStore()
    run_job(store, noaa)
    before = store.pointer()
    later = datetime(2026, 10, 16, 1, 25, tzinfo=UTC)
    noaa.add("24h", datetime(2026, 10, 16, 0, tzinfo=UTC), b"<html>oops</html>", '"bad"')

    assert run_job(store, noaa, later) is False  # cron log shows it
    assert store.pointer()["periods"] == before["periods"]


def test_bad_first_file_falls_back_to_an_older_issue():
    noaa = FakeNohrsc()
    noaa.add("24h", ISSUE_12Z, nohrsc_tif(peak=900), '"bad"')
    noaa.add("24h", ISSUE_12Z - timedelta(hours=12), nohrsc_tif(), '"ok"')
    store = FakeStore()
    assert run_job(store, noaa) is False  # bad file, and no 72 h at all
    assert store.pointer()["periods"]["24h"]["valid"] == "2026-10-15T00:00:00Z"


def test_nothing_published_writes_no_pointer():
    store = FakeStore()
    assert run_job(store, FakeNohrsc()) is False
    assert store.objects == {}


def test_nohrsc_outage_keeps_served_data(noaa):
    store = FakeStore()
    run_job(store, noaa)
    before = store.pointer()

    def down(request):
        raise httpx.ConnectError("down")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as client:
            return await job.run(store, client, ["colorado"], now=NOW + timedelta(hours=1), base=BASE)

    assert asyncio.run(go()) is False
    assert store.pointer() == before


def test_cleanup_deletes_only_old_unreferenced_revisions(noaa):
    store = FakeStore()
    run_job(store, noaa)
    served = {e["key"] for e in store.pointer()["periods"].values()}
    old = NOW - timedelta(days=8)
    store.objects["snowfall/colorado/24h/2026100712-deadbeef.tif"] = (b"x", old)
    store.objects["snowfall/colorado/24h/2026101400-cafecafe.tif"] = (b"x", NOW - timedelta(days=1))
    for key in served:  # served files are kept however old
        store.objects[key] = (store.objects[key][0], old)

    run_job(store, noaa, NOW + timedelta(hours=1))
    assert "snowfall/colorado/24h/2026100712-deadbeef.tif" not in store.objects
    assert "snowfall/colorado/24h/2026101400-cafecafe.tif" in store.objects
    assert served <= set(store.objects)


def test_dry_run_reads_but_writes_nothing(noaa):
    store = FakeStore()
    run_job(store, noaa)
    before = dict(store.objects)
    noaa.add("24h", ISSUE_12Z, nohrsc_tif(peak=14.0), '"a2"')
    store.objects["snowfall/colorado/24h/2026100712-deadbeef.tif"] = (b"x", NOW - timedelta(days=8))

    async def go():
        async with noaa.client() as client:
            return await job.run(job.DryRunStore(store), client, ["colorado"],
                                 now=NOW + timedelta(hours=1), base=BASE)

    assert asyncio.run(go()) is True
    assert store.objects == {**before, "snowfall/colorado/24h/2026100712-deadbeef.tif": (b"x", NOW - timedelta(days=8))}


# ── read_pointer ──────────────────────────────────────────────────────────────

def test_read_pointer_missing_then_cached(monkeypatch):
    calls = []

    async def fake_get(url, **kwargs):
        calls.append(url)
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(snowfall._HTTP, "get", fake_get)
    assert asyncio.run(snowfall.read_pointer("colorado")) is None
    assert asyncio.run(snowfall.read_pointer("colorado")) is None
    assert len(calls) == 1
    assert calls[0].endswith("/dem-cogs/snowfall/colorado/latest.json")


def test_read_pointer_returns_json(monkeypatch):
    async def fake_get(url, **kwargs):
        return httpx.Response(200, json={"periods": {}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(snowfall._HTTP, "get", fake_get)
    assert asyncio.run(snowfall.read_pointer("colorado")) == {"periods": {}}


# ── /tiles/snowfall ───────────────────────────────────────────────────────────

REV = "2026101512-a1b2c3d4"


def _entry(period: str, valid: str = "2026-10-15T12:00:00Z") -> dict:
    return {
        "valid": valid, "start": "2026-10-14T12:00:00Z", "revision": REV,
        "key": f"snowfall/colorado/{period}/{REV}.tif", "max_in": 12.4,
        "fetched": "2026-10-15T14:25:00Z", "etag": '"a"',
    }


@pytest.fixture
def pointer(monkeypatch) -> dict:
    """The served latest.json; tests edit it in place, or set it to an
    exception to make MinIO unreadable."""
    state = {"value": {"region": "colorado", "periods": {"24h": _entry("24h"), "72h": _entry("72h")}}}

    async def fake_read(region):
        if isinstance(state["value"], Exception):
            raise state["value"]
        return state["value"]

    monkeypatch.setattr(snowfall, "read_pointer", fake_read)
    return state


@pytest.fixture
def titiler(monkeypatch) -> dict:
    state: dict = {"status": 200, "calls": []}

    async def fake_get(url, params=None, **kwargs):
        state["calls"].append((url, params))
        return httpx.Response(
            state["status"], content=b"\x89PNG fake", headers={"content-type": "image/png"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(tiles._HTTP, "get", fake_get)
    return state


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


TILE = "/tiles/snowfall/24h/7/26/48?region=colorado"


def test_meta_lists_both_periods_with_valid_window_and_versioned_tiles(client, pointer):
    resp = client.get("/tiles/snowfall/meta?region=colorado")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["units"] == "in"
    assert body["attribution"] == snowfall.ATTRIBUTION
    assert body["bounds"] == list(CO.max_bounds)
    p24 = body["periods"]["24h"]
    assert p24["available"] is True
    assert (p24["start"], p24["valid"]) == ("2026-10-14T12:00:00Z", "2026-10-15T12:00:00Z")
    assert p24["tiles"] == f"/tiles/snowfall/24h/{{z}}/{{x}}/{{y}}?region=colorado&v={REV}"
    assert [s["color"] for s in body["legend"]] == [c for _, _, c in snowfall.SCALE]
    assert body["legend"][0]["min"] == 0.1 and body["legend"][-1]["max"] is None


def test_meta_before_first_ingest(client, pointer):
    pointer["value"] = None
    resp = client.get("/tiles/snowfall/meta?region=colorado")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert resp.json()["periods"] == {"24h": {"available": False}, "72h": {"available": False}}


def test_meta_rejects_unknown_region(client, pointer):
    assert client.get("/tiles/snowfall/meta?region=atlantis").status_code == 400


def test_legend_and_titiler_colormap_match():
    cmap = json.loads(snowfall.TITILER_COLORMAP)
    assert [tuple(rng) for rng, _ in cmap] == [(lo, hi) for lo, hi, _ in snowfall.SCALE]
    for (_, rgba), item in zip(cmap, snowfall.legend(), strict=True):
        r, g, b, a = rgba
        assert f"#{r:02x}{g:02x}{b:02x}" == item["color"] and a == 255
    # Contiguous steps, so no value between 0.1 and the top falls through.
    assert all(a[1] == b[0] for a, b in zip(snowfall.SCALE, snowfall.SCALE[1:], strict=False))


def test_current_revision_tile_is_cached_for_a_day(client, pointer, titiler):
    resp = client.get(f"{TILE}&v={REV}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    url, params = titiler["calls"][0]
    assert url.endswith("/cog/tiles/WebMercatorQuad/7/26/48.png")
    assert params["url"].endswith(f"/dem-cogs/snowfall/colorado/24h/{REV}.tif")
    assert params["url"].startswith("/vsicurl/")
    assert params["colormap"] == snowfall.TITILER_COLORMAP
    assert params["nodata"] == "-99999"
    assert params["reproject"] == "bilinear"

    # Second request comes from the worker cache.
    client.get(f"{TILE}&v={REV}")
    assert len(titiler["calls"]) == 1


@pytest.mark.parametrize("query", ["", "&v=2026101500-00000000", "&v=junk"])
def test_unversioned_or_old_revision_tile_is_short_cached(client, pointer, titiler, query):
    resp = client.get(TILE + query)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=900"
    assert titiler["calls"][0][1]["url"].endswith(f"{REV}.tif")  # always the served data


def test_new_revision_is_rendered_fresh(client, pointer, titiler):
    client.get(f"{TILE}&v={REV}")
    pointer["value"]["periods"]["24h"] = {**_entry("24h"), "revision": "r2", "key": "snowfall/colorado/24h/r2.tif"}
    resp = client.get(f"{TILE}&v=r2")
    assert resp.headers["cache-control"] == "public, max-age=86400"
    assert titiler["calls"][1][1]["url"].endswith("/r2.tif")


@pytest.mark.parametrize(
    "url, code",
    [
        ("/tiles/snowfall/48h/7/26/48?region=colorado", 422),
        ("/tiles/snowfall/24h/19/0/0?region=colorado", 400),
        ("/tiles/snowfall/24h/2/4/0?region=colorado", 400),
        ("/tiles/snowfall/24h/7/26/48?region=atlantis", 400),
    ],
)
def test_bad_tile_requests(client, pointer, titiler, url, code):
    assert client.get(url).status_code == code
    assert titiler["calls"] == []


def test_tile_before_first_ingest_is_503_no_store(client, pointer, titiler):
    pointer["value"] = {"periods": {"72h": _entry("72h")}}
    resp = client.get(TILE)
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"


def test_minio_unreadable_is_503_no_store(client, pointer, titiler):
    pointer["value"] = httpx.ConnectError("minio down")
    for url in (TILE, "/tiles/snowfall/meta?region=colorado"):
        resp = client.get(url)
        assert resp.status_code == 503
        assert resp.headers["cache-control"] == "no-store"


def test_tile_outside_the_cog_is_204(client, pointer, titiler):
    titiler["status"] = 404
    resp = client.get(f"{TILE}&v={REV}")
    assert resp.status_code == 204
    assert resp.headers["cache-control"] == "public, max-age=86400"


def test_titiler_error_is_502(client, pointer, titiler):
    titiler["status"] = 500
    assert client.get(TILE).status_code == 502


def test_snowfall_needs_a_tile_token_when_tokenless_is_off(client, pointer, titiler, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    for url in (TILE, "/tiles/snowfall/meta?region=colorado"):
        assert client.get(url).status_code == 401
    assert titiler["calls"] == []
    token = tile_tokens.issue("i:install-0123456789abcdef", tile_tokens.TIER_ANON)[0]
    assert client.get(f"{TILE}&t={token}").status_code == 200
    assert client.get(f"/tiles/snowfall/meta?region=colorado&t={token}").status_code == 200


# ── /status ───────────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_status_snowfall_fresh(pointer):
    fresh = _iso(datetime.now(UTC) - timedelta(hours=14))
    pointer["value"] = {"periods": {p: _entry(p, fresh) for p in snowfall.PERIODS}}
    health = asyncio.run(status._check_snowfall())
    assert (health.name, health.healthy, health.detail) == ("snowfall", True, None)


def test_status_snowfall_stale_or_missing(pointer):
    stale = _iso(datetime.now(UTC) - timedelta(hours=40))
    pointer["value"] = {"periods": {"24h": _entry("24h", stale)}}
    health = asyncio.run(status._check_snowfall())
    assert health.healthy is False
    assert "colorado 24h: valid" in health.detail and "40 h old" in health.detail
    assert "colorado 72h: no data" in health.detail


def test_status_snowfall_unreadable(pointer):
    pointer["value"] = httpx.ConnectError("minio down")
    assert asyncio.run(status._check_snowfall()).healthy is False
