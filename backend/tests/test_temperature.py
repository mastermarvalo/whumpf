"""Current temperature: the RTMA ingest, /tiles/temperature/*, and its
/status check. NOAA (AWS and NOMADS), MinIO and TiTiler are all faked."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pyproj import Transformer
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from tenacity import wait_none

from app import http_retry
from app.jobs import temperature_refresh as job
from app.main import app
from app.regions import REGIONS
from app.routers import status, weather_tiles
from app.services import temperature
from tests.weather_fakes import (
    FakeServer,
    FakeStore,
    fake_pointer,
    fake_titiler,
    reset_breakers,
)

CO = REGIONS["colorado"]
NOW = datetime(2026, 10, 16, 3, 50, tzinfo=UTC)
HOUR = datetime(2026, 10, 16, 3, tzinfo=UTC)
AWS = "https://aws.test"
NOMADS = "https://nomads.test/rtma/prod"
SOURCES = (("rtma-aws", AWS), ("rtma-nomads", NOMADS))
UPSTREAMS = ("rtma-aws", "rtma-nomads", "titiler", "minio")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, *UPSTREAMS)
    monkeypatch.setattr(temperature, "_pointers", {})
    weather_tiles.TEMPERATURE.cache.clear()
    yield
    for name in UPSTREAMS:
        http_retry._BREAKERS.pop(name, None)


# ── an RTMA-like message ──────────────────────────────────────────────────────
# A GeoTIFF on the NDFD Lambert grid with the band tags GDAL reports for an
# RTMA GRIB2 message (the job only looks at those, not the driver).

LCC = (
    "+proj=lcc +lat_0=25 +lon_0=-95 +lat_1=25 +lat_2=25 +x_0=0 +y_0=0 "
    "+a=6371200 +b=6371200 +units=m +no_defs"
)
RES_M = 2539.703


def rtma_message(
    hour: datetime = HOUR, *, celsius: float = 5.0, element: str = "TMP", unit: str = "[C]",
    crs: str = LCC, res: float = RES_M,
) -> bytes:
    """`celsius` everywhere over Colorado and around it, with -10 °C in the
    north-west corner."""
    to_lcc = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    xs, ys = to_lcc.transform([-111, -111, -99, -99], [35, 43, 35, 43])
    x0, y1 = min(xs), max(ys)
    w, h = int((max(xs) - x0) / res) + 1, int((y1 - min(ys)) / res) + 1
    data = np.full((h, w), celsius, dtype=np.float64)
    data[: h // 4, : w // 4] = -10.0
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff", width=w, height=h, count=1, dtype="float64", crs=crs,
            transform=from_origin(x0, y1, res, res),
        ) as dst:
            dst.write(data, 1)
            dst.update_tags(1, GRIB_ELEMENT=element, GRIB_UNIT=unit,
                            GRIB_VALID_TIME=str(int(hour.timestamp())))
        return mem.read()


def add_hour(server: FakeServer, base: str, hour: datetime, message: bytes) -> None:
    """The full analysis file with HGT and PRES before TMP and DPT after it,
    plus its .idx."""
    body = b"H" * 100 + b"P" * 50 + message + b"D" * 70
    d = f"d={hour:%Y%m%d%H}"
    idx = "\n".join([
        f"1:0:{d}:HGT:surface:anl:",
        f"2:100:{d}:PRES:surface:anl:",
        f"3:150:{d}:TMP:2 m above ground:anl:",
        f"4:{150 + len(message)}:{d}:DPT:2 m above ground:anl:",
    ]) + "\n"
    url = job.file_url(base, hour)
    server.add(url, body)
    server.add(f"{url}.idx", idx.encode())


# ── index + conversion ────────────────────────────────────────────────────────

def test_message_range_reads_the_tmp_line():
    idx = (
        "1:0:d=2026101603:HGT:surface:anl:\n"
        "3:14980236:d=2026101603:TMP:2 m above ground:anl:\n"
        "4:21065993:d=2026101603:DPT:2 m above ground:anl:\n"
    )
    assert job.message_range(idx, HOUR) == (14980236, 21065992)
    with pytest.raises(job.BadFile, match="not 2026101602"):
        job.message_range(idx, HOUR - timedelta(hours=1))
    with pytest.raises(job.BadFile, match="last message"):
        job.message_range(idx.rsplit("4:", 1)[0], HOUR)
    with pytest.raises(job.BadFile, match="no 2 m temperature"):
        job.message_range("1:0:d=2026101603:HGT:surface:anl:\n", HOUR)


def test_file_url_and_candidate_hours():
    assert job.file_url(AWS, HOUR) == f"{AWS}/rtma2p5.20261016/rtma2p5.t03z.2dvaranl_ndfd.grb2_wexp"
    hours = job.candidate_hours(NOW)
    assert hours[:2] == [HOUR, HOUR - timedelta(hours=1)] and len(hours) == 6


def test_to_cog_reprojects_to_lonlat_in_fahrenheit():
    cog, stats = job.to_cog(rtma_message(), HOUR, CO.max_bounds)
    assert stats == {"min_f": 14, "max_f": 41}
    with MemoryFile(cog) as mem, mem.open() as src:
        assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert src.crs.to_epsg() == 4326
        assert src.nodata == temperature.NODATA
        assert src.res == pytest.approx((0.025, 0.025))
        assert src.bounds.left == pytest.approx(CO.max_bounds[0])
        assert src.bounds.top == pytest.approx(CO.max_bounds[3])
        data = src.read(1)
        assert (data != temperature.NODATA).all()
        assert float(np.median(data)) == pytest.approx(41.0)


@pytest.mark.parametrize(
    "message, why",
    [
        (rtma_message(element="DPT"), "TMP"),
        (rtma_message(unit="[K]"), "TMP"),
        (rtma_message(HOUR - timedelta(hours=1)), "valid time"),
        (rtma_message(crs="EPSG:4326", res=0.025), "Lambert"),
        (rtma_message(res=3000.0), "2.5 km"),
        (rtma_message(celsius=80.0), "out of range"),
        (b"<html>Service Unavailable</html>", "readable"),
    ],
    ids=["element", "unit", "time", "crs", "res", "range", "html"],
)
def test_to_cog_rejects_anything_else(message, why):
    with pytest.raises(job.BadFile, match=why):
        job.to_cog(message, HOUR, CO.max_bounds)


# ── run ───────────────────────────────────────────────────────────────────────

POINTER = temperature.pointer_key("colorado")


def run_job(store, server, now=NOW) -> bool:
    async def go():
        async with server.client() as client:
            return await job.run(store, client, ["colorado"], now=now, sources=SOURCES)
    return asyncio.run(go())


@pytest.fixture
def noaa() -> FakeServer:
    s = FakeServer()
    add_hour(s, AWS, HOUR, rtma_message())
    add_hour(s, NOMADS, HOUR, rtma_message())
    return s


def test_run_publishes_the_newest_hour_with_a_range_request(noaa):
    store = FakeStore(NOW)
    assert run_job(store, noaa) is True
    e = store.json(POINTER)["current"]
    assert e["valid"] == "2026-10-16T03:00:00Z"
    assert e["revision"].startswith("2026101603-")
    assert e["key"] == f"temperature/colorado/{e['revision']}.tif"
    assert e["source"] == "rtma-aws"
    assert (e["min_f"], e["max_f"]) == (14, 41)
    assert store.get(e["key"]) is not None
    assert [u for u, _ in noaa.requests] == [
        job.file_url(AWS, HOUR) + ".idx", job.file_url(AWS, HOUR),
    ]


def test_served_hour_costs_no_requests(noaa):
    store = FakeStore(NOW)
    run_job(store, noaa)
    noaa.requests.clear()
    store.puts.clear()
    assert run_job(store, noaa, NOW + timedelta(minutes=5)) is True
    assert noaa.requests == [] and store.puts == []


def test_hour_not_posted_yet_falls_back_to_the_previous_one():
    noaa = FakeServer()
    add_hour(noaa, AWS, HOUR - timedelta(hours=1), rtma_message(HOUR - timedelta(hours=1)))
    store = FakeStore(NOW)
    assert run_job(store, noaa) is True
    assert store.json(POINTER)["current"]["valid"] == "2026-10-16T02:00:00Z"


def test_aws_down_uses_nomads_and_is_not_a_failure(noaa):
    noaa.down.add("aws.test")
    store = FakeStore(NOW)
    assert run_job(store, noaa) is True
    assert store.json(POINTER)["current"]["source"] == "rtma-nomads"


def test_both_down_keeps_served_data_and_fails(noaa):
    store = FakeStore(NOW)
    run_job(store, noaa)
    before = store.json(POINTER)
    noaa.down.update({"aws.test", "nomads.test"})
    assert run_job(store, noaa, NOW + timedelta(hours=1)) is False
    assert store.json(POINTER) == before


def test_bad_file_is_never_published(noaa):
    add_hour(noaa, AWS, HOUR, rtma_message(celsius=80.0))
    store = FakeStore(NOW)
    assert run_job(store, noaa) is False  # the rejection shows in the cron log
    assert store.json(POINTER)["current"]["source"] == "rtma-nomads"


def test_nothing_found_writes_no_pointer():
    store = FakeStore(NOW)
    assert run_job(store, FakeServer()) is False
    assert store.objects == {}


def test_cleanup_deletes_only_old_unreferenced_revisions(noaa):
    store = FakeStore(NOW)
    store.objects["temperature/colorado/2026100103-deadbeef.tif"] = (b"x", NOW - timedelta(days=8))
    store.objects["temperature/colorado/2026101502-cafecafe.tif"] = (b"x", NOW - timedelta(days=1))
    run_job(store, noaa)
    assert "temperature/colorado/2026100103-deadbeef.tif" not in store.objects
    assert "temperature/colorado/2026101502-cafecafe.tif" in store.objects


def test_dry_run_writes_nothing(noaa):
    store = FakeStore(NOW)

    async def go():
        async with noaa.client() as client:
            return await job.run(job.DryRunStore(store), client, ["colorado"], now=NOW, sources=SOURCES)

    assert asyncio.run(go()) is True
    assert store.objects == {}


# ── /tiles/temperature ────────────────────────────────────────────────────────

REV = "2026101603-a1b2c3d4"


def _iso_ago(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def pointer(monkeypatch) -> dict:
    return fake_pointer(monkeypatch, temperature, {"region": "colorado", "current": {
        "valid": _iso_ago(timedelta(minutes=50)), "revision": REV,
        "key": f"temperature/colorado/{REV}.tif", "source": "rtma-aws",
        "fetched": _iso_ago(timedelta(minutes=5)), "min_f": 14, "max_f": 41,
    }})


@pytest.fixture
def titiler(monkeypatch) -> dict:
    return fake_titiler(monkeypatch)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_meta_has_valid_hour_versioned_tiles_and_open_ended_legend(client, pointer):
    resp = client.get("/tiles/temperature/meta?region=colorado")
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["available"] is True and body["stale"] is False
    assert body["units"] == "F" and body["attribution"] == "NOAA/NWS RTMA"
    assert body["maxzoom"] == 9
    assert (body["min_f"], body["max_f"]) == (14, 41)
    assert body["tiles"] == f"/tiles/temperature/{{z}}/{{x}}/{{y}}?region=colorado&v={REV}"
    assert body["legend"][0] == {"min": None, "max": -10, "color": "#4a2a7a", "label": "< -10"}
    assert body["legend"][4]["label"] == "20–32" and body["legend"][5]["label"] == "32–40"
    assert body["legend"][-1]["label"] == "80+"

    pointer["value"]["current"]["valid"] = _iso_ago(timedelta(hours=4))
    temperature._pointers.clear()
    assert client.get("/tiles/temperature/meta?region=colorado").json()["stale"] is True


def test_legend_and_titiler_colormap_match():
    cmap = json.loads(temperature.TITILER_COLORMAP)
    assert [tuple(rng) for rng, _ in cmap] == [(lo, hi) for lo, hi, _ in temperature.SCALE]
    assert all(a[1] == b[0] for a, b in zip(temperature.SCALE, temperature.SCALE[1:], strict=False))


def test_tile_renders_the_served_revision(client, pointer, titiler):
    resp = client.get(f"/tiles/temperature/7/26/48?region=colorado&v={REV}")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    params = titiler["calls"][0][1]
    assert params["url"].endswith(f"/dem-cogs/temperature/colorado/{REV}.tif")
    assert params["colormap"] == temperature.TITILER_COLORMAP
    assert params["nodata"] == "-9999"
    resp = client.get("/tiles/temperature/7/26/48?region=colorado")
    assert resp.headers["cache-control"] == "public, max-age=900"


def test_no_data_is_503(client, pointer, titiler):
    pointer["value"] = None
    assert client.get("/tiles/temperature/7/26/48?region=colorado").status_code == 503
    assert client.get("/tiles/temperature/meta?region=colorado").json()["available"] is False


def test_status_temperature(pointer):
    assert asyncio.run(status._check_temperature()).healthy is True
    pointer["value"]["current"]["valid"] = _iso_ago(timedelta(hours=3, minutes=30))
    health = asyncio.run(status._check_temperature())
    assert health.healthy is False and "(3 h old)" in health.detail
