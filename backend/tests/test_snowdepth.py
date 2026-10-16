"""Snow depth: the SNODAS ingest, /tiles/snowdepth/*, and its /status check.
NSIDC, MinIO and TiTiler are all faked."""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.jobs import snowdepth_refresh as job
from app.main import app
from app.regions import REGIONS
from app.routers import status, weather_tiles
from app.services import snowdepth
from tests.weather_fakes import (
    FakeServer,
    FakeStore,
    fake_pointer,
    fake_titiler,
    reset_breakers,
)

CO = REGIONS["colorado"]
NOW = datetime(2026, 10, 15, 20, 35, tzinfo=UTC)
DAY = datetime(2026, 10, 15, 6, tzinfo=UTC)
BASE = "https://nsidc.test/G02158/masked"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, "nsidc", "titiler", "minio")
    monkeypatch.setattr(snowdepth, "_pointers", {})
    weather_tiles.SNOWDEPTH.cache.clear()
    yield
    for name in ("nsidc", "titiler", "minio"):
        http_retry._BREAKERS.pop(name, None)


# ── a SNODAS-like archive ─────────────────────────────────────────────────────

RES = 1 / 120
WEST, NORTH = -112.0, 44.0
COLS, ROWS = 12 * 120, 10 * 120  # -112..-100, 34..44


def snodas_tar(
    day: datetime = DAY, *, peak_mm: int = 2540, base_mm: int = 254, units: str = "Meters / 1000.000000",
    res: float = RES, rows: int = ROWS, all_nodata: bool = False, include_dat: bool = True,
    extra_members: bool = True,
) -> bytes:
    """`base_mm` everywhere (10 in), `peak_mm` near Tenmile (100 in), a 5×5
    block of -9999 inside the Colorado window."""
    data = np.full((ROWS, COLS), base_mm, dtype=">i2")
    data[int((NORTH - 39.08) * 120), int((-106.18 - WEST) * 120)] = peak_mm
    r0, c0 = int((NORTH - 41.4) * 120), int((-109.4 - WEST) * 120)
    data[r0:r0 + 5, c0:c0 + 5] = -9999
    if all_nodata:
        data[:] = -9999
    header = "\n".join([
        "Format version: NOHRSC GIS/RS raster file v1.1",
        f"Data units: {units}",
        "Data type: integer",
        "Data bytes per pixel: 2",
        "No data value: -9999.00000000000",
        f"Number of columns: {COLS}      ",
        f"Number of rows: {rows}      ",
        f"X-axis resolution: {res}",
        f"Y-axis resolution: {res}",
        f"Minimum x-axis coordinate: {WEST}",
        f"Maximum y-axis coordinate: {NORTH}",
        f"Start year: {day.year}", f"Start month: {day.month}", f"Start day: {day.day}",
        f"Start hour: {day.hour} ",
    ]) + "\n"
    stem = f"us_ssmv11036tS__T0001TTNATS{day:%Y%m%d}05HP001"
    members = {f"{stem}.txt.gz": gzip.compress(header.encode())}
    if include_dat:
        members[f"{stem}.dat.gz"] = gzip.compress(data.tobytes())
    if extra_members:  # SWE etc. must be ignored
        members[f"us_ssmv11034tS__T0001TTNATS{day:%Y%m%d}05HP001.dat.gz"] = gzip.compress(b"\0" * 64)
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def to_cog(body: bytes):
    header, grid = job.read_depth(body)
    return job.clip_to_cog(header, grid, CO.max_bounds)


# ── unpack + clip ─────────────────────────────────────────────────────────────

def test_clip_makes_a_region_cog_in_inches():
    cog, stats, valid = to_cog(snodas_tar())
    assert valid == DAY
    assert stats["max_in"] == 100.0
    assert stats["snow_share"] == 1.0
    with MemoryFile(cog) as mem, mem.open() as src:
        assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert src.crs.to_epsg() == 4326
        assert src.nodata == snowdepth.NODATA
        west, south, east, north = src.bounds
        assert west <= CO.max_bounds[0] + 1e-6 and south <= CO.max_bounds[1] + 1e-6
        assert east >= CO.max_bounds[2] - 1e-6 and north >= CO.max_bounds[3] - 1e-6
        assert east - west < 8.1 and north - south < 5.1
        data = src.read(1)
        assert (data == snowdepth.NODATA).sum() == 25
        assert float(data[data != snowdepth.NODATA].min()) == pytest.approx(10.0)


@pytest.mark.parametrize(
    "body, why",
    [
        (snodas_tar(units="Kelvin / 1.0"), "millimetres"),
        (snodas_tar(res=0.01), "pixels"),
        (snodas_tar(rows=ROWS + 1), "header says"),
        (snodas_tar(all_nodata=True), "has data"),
        (snodas_tar(peak_mm=32000), "out of range"),
        (snodas_tar(include_dat=False), "no snow depth"),
        (b"<html>Service Unavailable</html>", "tar"),
    ],
    ids=["units", "res", "size", "all-nodata", "range", "no-depth", "html"],
)
def test_rejects_files_that_are_not_the_grid(body, why):
    with pytest.raises(job.BadFile, match=why):
        to_cog(body)


def test_candidate_days_are_06z_newest_first():
    assert job.candidate_days(NOW)[:2] == [DAY, DAY - timedelta(days=1)]
    early = datetime(2026, 10, 15, 5, tzinfo=UTC)  # before today's 06Z
    assert job.candidate_days(early)[0] == DAY - timedelta(days=1)
    assert job.day_url(BASE, DAY) == f"{BASE}/2026/10_Oct/SNODAS_20261015.tar"


# ── run ───────────────────────────────────────────────────────────────────────

def run_job(store, server, now=NOW) -> bool:
    async def go():
        async with server.client() as client:
            return await job.run(store, client, ["colorado"], now=now, base=BASE)
    return asyncio.run(go())


@pytest.fixture
def nsidc() -> FakeServer:
    s = FakeServer()
    s.add(job.day_url(BASE, DAY - timedelta(days=1)), snodas_tar(DAY - timedelta(days=1)), '"y"')
    s.add(job.day_url(BASE, DAY), snodas_tar(), '"t"')
    return s


POINTER = snowdepth.pointer_key("colorado")


def test_run_publishes_newest_day(nsidc):
    store = FakeStore(NOW)
    assert run_job(store, nsidc) is True
    e = store.json(POINTER)["current"]
    assert e["valid"] == "2026-10-15T06:00:00Z"
    assert e["revision"].startswith("20261015-")
    assert e["key"] == f"snowdepth/colorado/{e['revision']}.tif"
    assert e["max_in"] == 100.0 and e["etag"] == '"t"'
    assert store.get(e["key"]) is not None
    assert nsidc.requests[0][0] == job.day_url(BASE, DAY)


def test_served_day_is_a_conditional_get(nsidc):
    store = FakeStore(NOW)
    run_job(store, nsidc)
    store.puts.clear()
    nsidc.requests.clear()
    assert run_job(store, nsidc, NOW + timedelta(hours=1)) is True
    assert store.puts == []
    assert nsidc.requests == [(job.day_url(BASE, DAY), '"t"')]


def test_before_todays_file_posts_the_newest_older_day_is_served(nsidc):
    del nsidc.files[job.day_url(BASE, DAY)]
    store = FakeStore(NOW)
    assert run_job(store, nsidc) is True
    assert store.json(POINTER)["current"]["valid"] == "2026-10-14T06:00:00Z"

    nsidc.add(job.day_url(BASE, DAY), snodas_tar(), '"t"')
    run_job(store, nsidc, NOW + timedelta(hours=1))
    assert store.json(POINTER)["current"]["valid"] == "2026-10-15T06:00:00Z"


def test_bad_newer_day_never_replaces_good_data(nsidc):
    store = FakeStore(NOW)
    run_job(store, nsidc)
    before = store.json(POINTER)["current"]
    later = NOW + timedelta(days=1)
    nsidc.add(job.day_url(BASE, DAY + timedelta(days=1)), b"<html>oops</html>", '"bad"')
    assert run_job(store, nsidc, later) is False
    assert store.json(POINTER)["current"] == before


def test_archive_for_the_wrong_day_is_rejected(nsidc):
    nsidc.add(job.day_url(BASE, DAY), snodas_tar(DAY - timedelta(days=1)), '"t"')
    store = FakeStore(NOW)
    assert run_job(store, nsidc) is False
    assert store.json(POINTER)["current"]["valid"] == "2026-10-14T06:00:00Z"


def test_outage_keeps_served_data(nsidc):
    store = FakeStore(NOW)
    run_job(store, nsidc)
    before = store.json(POINTER)
    nsidc.down.add("nsidc.test")
    assert run_job(store, nsidc, NOW + timedelta(hours=1)) is False
    assert store.json(POINTER) == before


def test_nothing_found_writes_no_pointer():
    store = FakeStore(NOW)
    assert run_job(store, FakeServer()) is False
    assert store.objects == {}


def test_cleanup_deletes_only_old_unreferenced_revisions(nsidc):
    store = FakeStore(NOW)
    run_job(store, nsidc)
    served = store.json(POINTER)["current"]["key"]
    store.objects["snowdepth/colorado/20261001-deadbeef.tif"] = (b"x", NOW - timedelta(days=8))
    store.objects["snowdepth/colorado/20261014-cafecafe.tif"] = (b"x", NOW - timedelta(days=1))
    store.objects[served] = (store.objects[served][0], NOW - timedelta(days=30))
    run_job(store, nsidc, NOW + timedelta(hours=1))
    assert "snowdepth/colorado/20261001-deadbeef.tif" not in store.objects
    assert "snowdepth/colorado/20261014-cafecafe.tif" in store.objects
    assert served in store.objects


def test_dry_run_writes_nothing(nsidc):
    store = FakeStore(NOW)

    async def go():
        async with nsidc.client() as client:
            return await job.run(job.DryRunStore(store), client, ["colorado"], now=NOW, base=BASE)

    assert asyncio.run(go()) is True
    assert store.objects == {}


# ── /tiles/snowdepth ──────────────────────────────────────────────────────────

REV = "20261015-a1b2c3d4"


def _entry(valid: str = "2026-10-15T06:00:00Z") -> dict:
    return {
        "valid": valid, "revision": REV, "key": f"snowdepth/colorado/{REV}.tif",
        "etag": '"t"', "fetched": "2026-10-15T20:35:00Z", "max_in": 100.0, "snow_share": 0.4,
    }


def _now_iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def pointer(monkeypatch) -> dict:
    return fake_pointer(monkeypatch, snowdepth, {"region": "colorado", "current": _entry(_now_iso(timedelta(hours=14)))})


@pytest.fixture
def titiler(monkeypatch) -> dict:
    return fake_titiler(monkeypatch)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


META = "/tiles/snowdepth/meta?region=colorado"
TILE = "/tiles/snowdepth/7/26/48?region=colorado"


def test_meta_has_valid_time_versioned_tiles_and_legend(client, pointer):
    resp = client.get(META)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["available"] is True and body["stale"] is False
    assert body["units"] == "in"
    assert body["attribution"] == "NOAA/NWS NOHRSC SNODAS"
    assert body["attribution_url"].startswith("https://nsidc.org/")
    assert body["bounds"] == list(CO.max_bounds)
    assert body["revision"] == REV and body["max_in"] == 100.0
    assert body["tiles"] == f"/tiles/snowdepth/{{z}}/{{x}}/{{y}}?region=colorado&v={REV}"
    assert "key" not in body and "etag" not in body
    assert [s["color"] for s in body["legend"]] == [c for _, _, c in snowdepth.SCALE]
    assert body["legend"][-1] == {"min": 120, "max": None, "color": "#7d1670", "label": "120+"}


def test_meta_marks_old_data_stale(client, pointer):
    pointer["value"]["current"]["valid"] = _now_iso(timedelta(hours=50))
    assert client.get(META).json()["stale"] is True


def test_meta_before_first_ingest(client, pointer):
    pointer["value"] = None
    resp = client.get(META)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"
    body = resp.json()
    assert body["available"] is False and "tiles" not in body


def test_meta_rejects_unknown_region(client, pointer):
    assert client.get("/tiles/snowdepth/meta?region=atlantis").status_code == 400


def test_legend_and_titiler_colormap_match():
    cmap = json.loads(snowdepth.TITILER_COLORMAP)
    assert [tuple(rng) for rng, _ in cmap] == [(lo, hi) for lo, hi, _ in snowdepth.SCALE]
    assert all(a[1] == b[0] for a, b in zip(snowdepth.SCALE, snowdepth.SCALE[1:], strict=False))


def test_current_revision_tile_is_cached_for_a_day(client, pointer, titiler):
    resp = client.get(f"{TILE}&v={REV}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    url, params = titiler["calls"][0]
    assert url.endswith("/cog/tiles/WebMercatorQuad/7/26/48.png")
    assert params["url"].endswith(f"/dem-cogs/snowdepth/colorado/{REV}.tif")
    assert params["colormap"] == snowdepth.TITILER_COLORMAP
    assert params["nodata"] == "-9999"
    client.get(f"{TILE}&v={REV}")
    assert len(titiler["calls"]) == 1  # worker cache


@pytest.mark.parametrize("query", ["", "&v=20261014-00000000"])
def test_unversioned_or_old_tile_is_short_cached(client, pointer, titiler, query):
    resp = client.get(TILE + query)
    assert resp.headers["cache-control"] == "public, max-age=900"
    assert titiler["calls"][0][1]["url"].endswith(f"{REV}.tif")


@pytest.mark.parametrize(
    "url, code",
    [
        ("/tiles/snowdepth/19/0/0?region=colorado", 400),
        ("/tiles/snowdepth/2/4/0?region=colorado", 400),
        ("/tiles/snowdepth/7/26/48?region=atlantis", 400),
    ],
)
def test_bad_tile_requests(client, pointer, titiler, url, code):
    assert client.get(url).status_code == code
    assert titiler["calls"] == []


def test_no_data_or_minio_down_is_503_no_store(client, pointer, titiler):
    pointer["value"] = None
    resp = client.get(TILE)
    assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"
    pointer["value"] = httpx.ConnectError("minio down")
    for url in (TILE, META):
        resp = client.get(url)
        assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"


def test_tile_outside_the_cog_is_204_and_titiler_error_502(client, pointer, titiler):
    titiler["status"] = 404
    assert client.get(f"{TILE}&v={REV}").status_code == 204
    titiler["status"] = 500
    assert client.get("/tiles/snowdepth/7/27/48?region=colorado").status_code == 502


def test_snowdepth_needs_a_tile_token_when_tokenless_is_off(client, pointer, titiler, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    for url in (TILE, META):
        assert client.get(url).status_code == 401
    token = tile_tokens.issue("i:install-0123456789abcdef", tile_tokens.TIER_ANON)[0]
    assert client.get(f"{TILE}&t={token}").status_code == 200
    assert client.get(f"{META}&t={token}").status_code == 200


# ── /status ───────────────────────────────────────────────────────────────────

def test_status_snowdepth_fresh_stale_missing_unreadable(pointer):
    health = asyncio.run(status._check_snowdepth())
    assert (health.name, health.healthy, health.detail) == ("snowdepth", True, None)

    pointer["value"]["current"]["valid"] = _now_iso(timedelta(hours=60))
    health = asyncio.run(status._check_snowdepth())
    assert health.healthy is False and "colorado: valid" in health.detail and "60 h old" in health.detail

    pointer["value"] = {}
    assert asyncio.run(status._check_snowdepth()).detail == "colorado: no data"

    pointer["value"] = httpx.ConnectError("minio down")
    assert asyncio.run(status._check_snowdepth()).healthy is False
