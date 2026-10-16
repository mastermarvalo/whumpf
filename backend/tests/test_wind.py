"""Wind now: the RTMA wind part of the hourly RTMA job, /tiles/wind/*, and
its /status check. NOAA (AWS and NOMADS) and MinIO are faked."""

from __future__ import annotations

import asyncio
import gzip
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from limits import parse
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.jobs import temperature_refresh as job
from app.main import app
from app.regions import REGIONS
from app.routers import status
from app.services import temperature, wind
from tests.weather_fakes import (
    FakeServer,
    FakeStore,
    add_rtma_hour,
    fake_pointer,
    reset_breakers,
    rtma_message,
    wind_messages,
)

CO = REGIONS["colorado"]
NOW = datetime(2026, 10, 16, 3, 50, tzinfo=UTC)
HOUR = datetime(2026, 10, 16, 3, tzinfo=UTC)
AWS = "https://aws.test"
NOMADS = "https://nomads.test/rtma/prod"
SOURCES = (("rtma-aws", AWS), ("rtma-nomads", NOMADS))
UPSTREAMS = ("rtma-aws", "rtma-nomads", "minio")
WIND_POINTER = wind.pointer_key("colorado")
TEMP_POINTER = temperature.pointer_key("colorado")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, *UPSTREAMS)
    monkeypatch.setattr(wind, "_pointers", {})
    monkeypatch.setattr(temperature, "_pointers", {})
    wind._arrows.clear()
    yield
    for name in UPSTREAMS:
        http_retry._BREAKERS.pop(name, None)


def arrows(body: bytes) -> dict:
    return json.loads(gzip.decompress(body))


def build(hour: datetime = HOUR, **kwargs) -> tuple[dict, dict]:
    m = wind_messages(hour, **kwargs)
    body, stats = job.to_arrows(m["WDIR"], m["WIND"], m["GUST"], hour, CO.max_bounds)
    return arrows(body), stats


# ── index ─────────────────────────────────────────────────────────────────────

# The real RTMA index for 2026-10-16 03Z.
REAL_IDX = """\
1:0:d=2026101603:HGT:surface:anl:
2:7490118:d=2026101603:PRES:surface:anl:
3:14980236:d=2026101603:TMP:2 m above ground:anl:
4:21065993:d=2026101603:DPT:2 m above ground:anl:
5:27151750:d=2026101603:UGRD:10 m above ground:anl:
6:32769386:d=2026101603:VGRD:10 m above ground:anl:
7:38855143:d=2026101603:SPFH:2 m above ground:anl:
8:45877141:d=2026101603:WDIR:10 m above ground:anl:
9:51494777:d=2026101603:WIND:10 m above ground:anl:
10:57112413:d=2026101603:GUST:10 m above ground:anl:
11:62730049:d=2026101603:VIS:surface:anl:
"""


def test_the_three_wind_messages_are_one_request():
    ranges = [job.message_range(REAL_IDX, HOUR, f) for f in (job._FIELD, job.WDIR, job.WIND, job.GUST)]
    assert ranges[1:] == [(45877141, 51494776), (51494777, 57112412), (57112413, 62730048)]
    assert job.merge_ranges(ranges) == [(14980236, 21065992), (45877141, 62730048)]
    with pytest.raises(job.BadFile, match="no 10 m gust"):
        job.message_range(REAL_IDX.replace(":GUST:", ":XXXX:"), HOUR, job.GUST)


def test_merge_ranges():
    assert job.merge_ranges([(30, 39), (0, 9), (10, 19)]) == [(0, 19), (30, 39)]
    assert job.merge_ranges([(0, 20), (5, 9)]) == [(0, 20)]
    assert job.merge_ranges([]) == []


# ── arrows ────────────────────────────────────────────────────────────────────

def test_arrows_are_a_thinned_point_grid_in_mph():
    fc, stats = build(direction=270, speed=10, gust=15)
    assert fc["type"] == "FeatureCollection"
    assert fc["valid"] == "2026-10-16T03:00:00Z" and fc["units"] == "mph"
    feats = fc["features"]
    # Colorado is 8° × 5°: 160 × 100 points at 0.05°.
    assert len(feats) == 160 * 100 == stats["points"]
    first = feats[0]
    assert first["geometry"] == {"type": "Point", "coordinates": [-109.475, 41.475]}
    # 10 m/s = 22.4 mph, 15 m/s = 33.6 mph.
    assert first["properties"] == {"dir": 270, "mph": 22, "gust_mph": 34, "lvl": 0}
    assert stats["max_mph"] == 22 and stats["max_gust_mph"] == 34
    # Every 4th row and column is lvl 0 (0.2°), every 2nd lvl 1 (0.1°).
    lvls = Counter(f["properties"]["lvl"] for f in feats)
    assert lvls == {0: 40 * 25, 1: 80 * 50 - 40 * 25, 2: 160 * 100 - 80 * 50}
    assert {f["properties"]["dir"] for f in feats} == {270}


def test_direction_is_averaged_as_a_vector_not_a_number():
    """Neighbouring cells from 350° and 10° are wind from the north (0°),
    not from the south (180°, the plain average)."""
    def stripes(data):
        data[:, ::2] = 350.0
        data[:, 1::2] = 10.0

    m = wind_messages(HOUR)
    m["WDIR"] = rtma_message(HOUR, corner=None, element="WDIR", unit="[deg true]", fill=stripes)
    body, _ = job.to_arrows(m["WDIR"], m["WIND"], m["GUST"], HOUR, CO.max_bounds)
    dirs = {f["properties"]["dir"] for f in arrows(body)["features"]}
    assert dirs <= {358, 359, 0, 1, 2}


def test_calm_air_has_no_direction():
    fc, stats = build(speed=0, gust=0)
    assert {f["properties"]["dir"] for f in fc["features"]} == {None}
    assert stats["max_mph"] == 0


def test_missing_gusts_are_null():
    m = wind_messages(HOUR)
    m["GUST"] = rtma_message(HOUR, value=-9999.0, corner=None, element="GUST", unit="[m/s]", nodata=-9999.0)
    body, stats = job.to_arrows(m["WDIR"], m["WIND"], m["GUST"], HOUR, CO.max_bounds)
    assert {f["properties"]["gust_mph"] for f in arrows(body)["features"]} == {None}
    assert stats["max_gust_mph"] is None


_FIELDS = {"WDIR": ("WDIR", "[deg true]", 270.0), "WIND": ("WIND", "[m/s]", 10.0), "GUST": ("GUST", "[m/s]", 15.0)}


def _bad(field: str, hour: datetime = HOUR, **kwargs) -> dict[str, bytes]:
    """Good wind messages except `field`, which gets `kwargs`."""
    element, unit, value = _FIELDS[field]
    args = {"element": element, "unit": unit, "value": value, "corner": None, **kwargs}
    return {**wind_messages(HOUR), field: rtma_message(hour, **args)}


@pytest.mark.parametrize(
    "messages, why",
    [
        (_bad("WDIR", unit="[m/s]"), "WDIR band in \\[deg true\\]"),
        (_bad("WIND", element="UGRD"), "WIND band"),
        (_bad("GUST", hour=HOUR - timedelta(hours=1)), "valid time"),
        (_bad("WIND", crs="EPSG:4326", res=0.025), "Lambert"),
        (_bad("GUST", res=3000.0), "2.5 km"),
        (_bad("WDIR", value=400.0), "directions .* out of range"),
        (_bad("WIND", value=100.0), "speed .* out of range"),
        (_bad("GUST", value=-5.0), "gust .* out of range"),
        ({**wind_messages(HOUR), "GUST": b"<html>Service Unavailable</html>"}, "readable"),
    ],
    ids=["unit", "element", "time", "crs", "res", "direction", "speed", "gust", "html"],
)
def test_arrows_reject_anything_else(messages, why):
    with pytest.raises(job.BadFile, match=why):
        job.to_arrows(messages["WDIR"], messages["WIND"], messages["GUST"], HOUR, CO.max_bounds)


# ── run ───────────────────────────────────────────────────────────────────────

def add_hour(server: FakeServer, base: str, hour: datetime = HOUR, **wind_kwargs) -> None:
    add_rtma_hour(server, job.file_url(base, hour), hour,
                  {"TMP": rtma_message(hour), **wind_messages(hour, **wind_kwargs)})


def run_job(store, server, now=NOW) -> bool:
    async def go():
        async with server.client() as client:
            return await job.run(store, client, ["colorado"], now=now, sources=SOURCES)
    return asyncio.run(go())


@pytest.fixture
def noaa() -> FakeServer:
    s = FakeServer()
    add_hour(s, AWS)
    add_hour(s, NOMADS)
    return s


def test_run_publishes_wind_with_one_extra_range(noaa):
    store = FakeStore(NOW)
    assert run_job(store, noaa) is True
    e = store.json(WIND_POINTER)["current"]
    assert e["valid"] == "2026-10-16T03:00:00Z"
    assert e["revision"].startswith("2026101603-")
    assert e["key"] == f"wind/colorado/{e['revision']}.geojson.gz"
    assert e["source"] == "rtma-aws"
    assert (e["max_mph"], e["max_gust_mph"], e["points"]) == (22, 34, 16000)
    assert arrows(store.get(e["key"]))["features"][0]["properties"]["dir"] == 270
    # Temperature and wind come from one index and two ranges.
    assert "current" in store.json(TEMP_POINTER)
    url = job.file_url(AWS, HOUR)
    assert [u for u, _ in noaa.requests] == [url + ".idx", url, url]
    idx = noaa.files[url + ".idx"][0].decode()
    wdir_lo = job.message_range(idx, HOUR, job.WDIR)[0]
    gust_hi = job.message_range(idx, HOUR, job.GUST)[1]
    assert noaa.ranges[2] == f"bytes={wdir_lo}-{gust_hi}"


def test_bad_wind_never_publishes_but_temperature_does(noaa):
    add_hour(noaa, AWS, speed=100.0)
    store = FakeStore(NOW)
    assert run_job(store, noaa) is False  # the rejection shows in the cron log
    assert store.json(TEMP_POINTER)["current"]["source"] == "rtma-aws"
    assert store.json(WIND_POINTER)["current"]["source"] == "rtma-nomads"


def test_wind_missing_from_the_file_still_publishes_temperature():
    noaa = FakeServer()
    for base in (AWS, NOMADS):
        add_rtma_hour(noaa, job.file_url(base, HOUR), HOUR, {"TMP": rtma_message(HOUR)})
    store = FakeStore(NOW)
    assert run_job(store, noaa) is False
    assert "current" in store.json(TEMP_POINTER)
    assert store.get(WIND_POINTER) is None


def test_a_missed_wind_hour_is_filled_in_without_refetching_temperature(noaa):
    store = FakeStore(NOW)
    run_job(store, noaa)
    temp_before = store.json(TEMP_POINTER)
    del store.objects[WIND_POINTER]
    noaa.requests.clear()
    noaa.ranges.clear()
    assert run_job(store, noaa, NOW + timedelta(minutes=5)) is True
    assert "current" in store.json(WIND_POINTER)
    assert store.json(TEMP_POINTER)["current"] == temp_before["current"]
    assert len(noaa.requests) == 2  # the index and the wind range only


def test_served_hour_costs_no_requests(noaa):
    store = FakeStore(NOW)
    run_job(store, noaa)
    noaa.requests.clear()
    assert run_job(store, noaa, NOW + timedelta(minutes=5)) is True
    assert noaa.requests == []


def test_cleanup_deletes_old_arrow_files(noaa):
    store = FakeStore(NOW)
    store.objects["wind/colorado/2026100103-deadbeef.geojson.gz"] = (b"x", NOW - timedelta(days=8))
    store.objects["wind/colorado/2026101502-cafecafe.geojson.gz"] = (b"x", NOW - timedelta(days=1))
    run_job(store, noaa)
    assert "wind/colorado/2026100103-deadbeef.geojson.gz" not in store.objects
    assert "wind/colorado/2026101502-cafecafe.geojson.gz" in store.objects


# ── /tiles/wind ───────────────────────────────────────────────────────────────

REV = "2026101603-a1b2c3d4"
META = "/tiles/wind/meta?region=colorado"
FILE = f"/tiles/wind/{REV}.geojson?region=colorado"


def _iso_ago(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def pointer(monkeypatch) -> dict:
    return fake_pointer(monkeypatch, wind, {"region": "colorado", "current": {
        "valid": _iso_ago(timedelta(minutes=50)), "revision": REV,
        "key": wind.arrows_key("colorado", REV), "source": "rtma-aws",
        "fetched": _iso_ago(timedelta(minutes=5)),
        "max_mph": 22, "max_gust_mph": 34, "points": 16000,
    }})


@pytest.fixture
def minio(monkeypatch) -> dict:
    """The dem-cogs bucket over HTTP: state["files"][key] = bytes."""
    body, _ = job.to_arrows(*wind_messages(HOUR).values(), HOUR, CO.max_bounds)
    state: dict = {"files": {wind.arrows_key("colorado", REV): body}, "calls": [], "down": False}
    settings = get_settings()
    base = f"{settings.s3_endpoint}/{settings.s3_bucket_dem_cogs}/"

    async def fake_get(url, **kwargs):
        state["calls"].append(url)
        request = httpx.Request("GET", url)
        if state["down"]:
            raise httpx.ConnectError("minio down", request=request)
        found = state["files"].get(url.removeprefix(base))
        return httpx.Response(200 if found else 404, content=found or b"", request=request)

    monkeypatch.setattr(wind._HTTP, "get", fake_get)
    return state


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_meta_lists_the_hour_levels_and_units(client, pointer):
    resp = client.get(META)
    assert resp.headers["cache-control"] == "public, max-age=300"
    body = resp.json()
    assert body["available"] is True and body["stale"] is False
    assert body["units"] == "mph" and body["direction"] == "from"
    assert body["attribution"] == "NOAA/NWS RTMA"
    assert body["bounds"] == list(CO.max_bounds)
    assert (body["max_mph"], body["max_gust_mph"], body["points"]) == (22, 34, 16000)
    assert body["revision"] == REV and "key" not in body
    assert body["hours"] == [{"time": body["valid"], "kind": "analysis", "url": FILE}]
    assert [lvl["max_lvl"] for lvl in body["levels"]] == [0, 1, 2]
    assert body["levels"][0]["minzoom"] == 0 and body["levels"][-1]["step_deg"] == 0.05

    pointer["value"]["current"]["valid"] = _iso_ago(timedelta(hours=4))
    assert client.get(META).json()["stale"] is True


def test_meta_without_data(client, pointer):
    pointer["value"] = None
    resp = client.get(META)
    assert resp.headers["cache-control"] == "public, max-age=60"
    body = resp.json()
    assert body["available"] is False and body["hours"] == [] and "stale" not in body


def test_arrows_are_served_gzipped_and_cached_for_a_day(client, minio):
    resp = client.get(FILE)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/geo+json"
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["cache-control"] == "public, max-age=86400"
    fc = resp.json()  # the test client un-gzips it
    assert len(fc["features"]) == 16000
    assert client.get(FILE).status_code == 200
    assert len(minio["calls"]) == 1  # kept in the worker after the first read


def test_unknown_or_malformed_revision_is_404(client, minio):
    resp = client.get("/tiles/wind/2026101503-00000000.geojson?region=colorado")
    assert resp.status_code == 404 and resp.headers["cache-control"] == "public, max-age=60"
    for bad in ("latest", "..%2Flatest", "2026101603-A1B2C3D4", "2026101603-a1b2c3d4x"):
        assert client.get(f"/tiles/wind/{bad}.geojson?region=colorado").status_code == 404
    assert len(minio["calls"]) == 1  # malformed ones never reach MinIO


def test_bad_region_is_400(client, pointer, minio):
    assert client.get("/tiles/wind/meta?region=atlantis").status_code == 400
    assert client.get(f"/tiles/wind/{REV}.geojson?region=atlantis").status_code == 400


def test_minio_down_is_503_no_store(client, pointer, minio):
    pointer["value"] = httpx.ConnectError("minio down")
    minio["down"] = True
    for url in (META, FILE):
        resp = client.get(url)
        assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"


def _token() -> str:
    return tile_tokens.issue(f"i:install-{uuid.uuid4().hex[:16]}", tile_tokens.TIER_ANON)[0]


def test_wind_needs_a_tile_token_when_tokenless_is_off(client, pointer, minio, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    for url in (META, FILE):
        assert client.get(url).status_code == 401
        assert client.get(f"{url}&t=forged").status_code == 401
        assert client.get(f"{url}&t={_token()}").status_code == 200


def test_wind_counts_against_the_tile_limit(client, pointer, minio, monkeypatch):
    monkeypatch.setitem(tile_tokens.BUCKETS, "anon", (parse("2/minute"),))
    token = _token()
    codes = [client.get(f"{url}&t={token}").status_code for url in (META, FILE, FILE)]
    assert codes == [200, 200, 429]


def test_status_wind(pointer):
    assert asyncio.run(status._check_wind()).healthy is True
    pointer["value"]["current"]["valid"] = _iso_ago(timedelta(hours=3, minutes=30))
    health = asyncio.run(status._check_wind())
    assert health.healthy is False and "(3 h old)" in health.detail
