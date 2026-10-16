"""Radar: the MRMS frame job, /tiles/radar/*, and its /status check. MRMS
(NCEP and AWS), MinIO and TiTiler are all faked."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.jobs import radar_refresh as job
from app.main import app
from app.regions import REGIONS
from app.routers import status, weather_tiles
from app.services import radar
from tests.weather_fakes import (
    FakeServer,
    FakeStore,
    fake_pointer,
    fake_titiler,
    reset_breakers,
)

CO = REGIONS["colorado"]
NOW = datetime(2026, 10, 16, 3, 50, tzinfo=UTC)
NCEP = "https://ncep.test/2D/MergedBaseReflectivityQC"
AWS = "https://aws.test"
SOURCES = (
    ("mrms-ncep", NCEP, job.list_ncep),
    ("mrms-aws", AWS, job.list_aws),
)
UPSTREAMS = ("mrms-ncep", "mrms-aws", "titiler", "minio")


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    reset_breakers(monkeypatch, *UPSTREAMS)
    monkeypatch.setattr(radar, "_pointers", {})
    weather_tiles._RADAR_CACHE.clear()
    yield
    for name in UPSTREAMS:
        http_retry._BREAKERS.pop(name, None)


# ── an MRMS-like file ─────────────────────────────────────────────────────────
# A gzipped GeoTIFF with the band tags GDAL reports for an MRMS GRIB2 file
# (the job only looks at those, not the driver).

def mrms_gz(
    observed: datetime, *, storm: float = 42.5, unit: str = "[dBZ]", res: float = 0.01,
    crs: str = "EPSG:4326", coverage: bool = True, light: bool = True,
) -> bytes:
    """No echo (-99, as MRMS writes it) over -112..-100, 34..44, a storm near
    Tenmile, 2 dBZ drizzle (under the 5 dBZ cut), 0 dBZ and a -999 (outside
    radar coverage) corner."""
    w, h = round(12 / res), round(10 / res)
    data = np.full((h, w), -99.0, dtype=np.float32)
    t = from_origin(-112, 44, res, res)
    row, col = int((44 - 39.1) / res), int((-106.2 + 112) / res)
    data[row:row + 20, col:col + 20] = storm
    r0, c0 = int((44 - 41.4) / res), int((-109.4 + 112) / res)
    data[r0:r0 + 5, c0:c0 + 5] = -999
    if light:
        data[row + 30:row + 40, col:col + 10] = 2.0
        data[r0 + 10:r0 + 15, c0:c0 + 5] = 0.0
    if not coverage:
        data[:] = -999
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", width=w, height=h, count=1, dtype="float32",
                      crs=crs, transform=t) as dst:
            dst.write(data, 1)
            dst.update_tags(1, GRIB_UNIT=unit, GRIB_VALID_TIME=str(int(observed.timestamp())))
        return gzip.compress(mem.read(), compresslevel=1)


def mrms_name(t: datetime) -> str:
    return f"MRMS_MergedBaseReflectivityQC_00.50_{t:%Y%m%d-%H%M%S}.grib2.gz"


class Mrms(FakeServer):
    """Both sources, with listings built from the files added."""

    def __init__(self) -> None:
        super().__init__()
        self.ncep: dict[datetime, bytes] = {}
        self.aws: dict[datetime, bytes] = {}

    def put(self, t: datetime, body: bytes | None = None, *, ncep=True, aws=True) -> None:
        body = body if body is not None else mrms_gz(t)
        if ncep:
            self.ncep[t] = body
        if aws:
            self.aws[t] = body
        self.rebuild()

    def rebuild(self) -> None:
        self.files.clear()
        html = "".join(f'<a href="{mrms_name(t)}">{mrms_name(t)}</a>\n' for t in sorted(self.ncep))
        html += '<a href="MRMS_MergedBaseReflectivityQC.latest.grib2.gz">latest</a>\n'
        self.add(f"{NCEP}/", html.encode())
        for t, body in self.ncep.items():
            self.add(f"{NCEP}/{mrms_name(t)}", body)
        for day in {t.date() for t in self.aws} | {NOW.date()}:
            keys = [
                f"{job.AWS_PREFIX}/{t:%Y%m%d}/{mrms_name(t)}"
                for t in sorted(self.aws) if t.date() == day
            ]
            xml = "<ListBucketResult>" + "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys) + "</ListBucketResult>"
            self.add(f"{AWS}/?list-type=2&prefix={job.AWS_PREFIX}/{day:%Y%m%d}/", xml.encode())
            for k, t in zip(keys, [t for t in sorted(self.aws) if t.date() == day], strict=True):
                self.add(f"{AWS}/{k}", self.aws[t])


FRAMES = [datetime(2026, 10, 16, 3, 40, tzinfo=UTC) - timedelta(minutes=10 * i) for i in range(12)]


def file_for(frame: datetime) -> datetime:
    """MRMS file times jitter around even minutes; this one is 1 m 48 s
    before the mark."""
    return frame - timedelta(seconds=108)


@pytest.fixture(scope="module")
def bodies() -> dict[datetime, bytes]:
    return {file_for(f): mrms_gz(file_for(f)) for f in FRAMES}


@pytest.fixture
def mrms(bodies) -> Mrms:
    m = Mrms()
    for t, body in bodies.items():
        m.ncep[t] = m.aws[t] = body
    # A newer file than the newest frame's: not ready (03:50 is under 3 min old).
    m.ncep[NOW - timedelta(seconds=30)] = mrms_gz(NOW - timedelta(seconds=30))
    m.rebuild()
    return m


# ── frames + files ────────────────────────────────────────────────────────────

def test_frame_times_are_ready_10_minute_marks_in_the_window():
    assert job.frame_times(NOW) == FRAMES  # 03:40 back to 01:50
    assert job.frame_times(NOW + timedelta(minutes=2, seconds=59))[0] == FRAMES[0]
    assert job.frame_times(NOW + timedelta(minutes=3))[0] == NOW


def test_pick_file_takes_the_newest_up_to_the_mark():
    frame = FRAMES[0]
    files = {
        frame - timedelta(minutes=9): "a",
        frame - timedelta(minutes=2): "b",
        frame: "c",
        frame + timedelta(seconds=5): "d",
        frame - timedelta(minutes=10): "e",
    }
    assert job.pick_file(files, frame) == (frame, "c")
    del files[frame]
    assert job.pick_file(files, frame) == (frame - timedelta(minutes=2), "b")
    assert job.pick_file({frame - timedelta(minutes=10): "e"}, frame) is None


def test_clip_makes_a_region_cog_with_no_echo_as_nodata():
    t = file_for(FRAMES[0])
    cog, stats = job.clip_to_cog(mrms_gz(t), t, CO.max_bounds)
    assert stats["max_dbz"] == 42.5
    assert 0 < stats["echo_share"] < 0.01
    with MemoryFile(cog) as mem, mem.open() as src:
        assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert src.crs.to_epsg() == 4326 and src.nodata == radar.NODATA
        assert src.width == 800 and src.height == 500
        data = src.read(1)
        assert set(np.unique(data)) == {radar.NODATA, np.float32(42.5)}  # -999, -99, 0, 2 all gone


def test_clear_sky_has_no_max_and_an_empty_cog():
    t = file_for(FRAMES[0])
    cog, stats = job.clip_to_cog(mrms_gz(t, storm=-99.0, light=False), t, CO.max_bounds)
    assert stats == {"max_dbz": None, "echo_share": 0.0}
    with MemoryFile(cog) as mem, mem.open() as src:
        assert (src.read(1) == radar.NODATA).all()


def test_echo_under_5_dbz_is_measured_but_not_drawn():
    t = file_for(FRAMES[0])
    cog, stats = job.clip_to_cog(mrms_gz(t, storm=-99.0), t, CO.max_bounds)
    assert stats == {"max_dbz": 2.0, "echo_share": 0.0}
    with MemoryFile(cog) as mem, mem.open() as src:
        assert (src.read(1) == radar.NODATA).all()


@pytest.mark.parametrize(
    "body, why",
    [
        (mrms_gz(file_for(FRAMES[0]), unit="[K]"), "dBZ"),
        (mrms_gz(file_for(FRAMES[1])), "valid time"),
        (mrms_gz(file_for(FRAMES[0]), res=0.02), "0.01"),
        (mrms_gz(file_for(FRAMES[0]), crs="EPSG:3857"), "lon/lat"),
        (mrms_gz(file_for(FRAMES[0]), storm=150), "out of range"),
        (mrms_gz(file_for(FRAMES[0]), coverage=False), "coverage"),
        (gzip.compress(b"<html>oops</html>"), "readable"),
        (b"<html>oops</html>", "gzip"),
    ],
    ids=["unit", "time", "res", "crs", "range", "coverage", "html-gz", "html"],
)
def test_clip_rejects_anything_else(body, why):
    with pytest.raises(job.BadFile, match=why):
        job.clip_to_cog(body, file_for(FRAMES[0]), CO.max_bounds)


# ── run ───────────────────────────────────────────────────────────────────────

POINTER = radar.pointer_key("colorado")


def run_job(store, server, now=NOW) -> bool:
    async def go():
        async with server.client() as client:
            return await job.run(store, client, ["colorado"], now=now, sources=SOURCES)
    return asyncio.run(go())


def test_run_fills_every_frame_of_the_window(mrms):
    store = FakeStore(NOW)
    assert run_job(store, mrms) is True
    frames = store.json(POINTER)["frames"]
    assert [f["time"] for f in frames] == [f"{t:%Y-%m-%dT%H:%M:00Z}" for t in reversed(FRAMES)]
    newest = frames[-1]
    assert newest["observed"] == "2026-10-16T03:38:12Z"
    assert newest["source"] == "mrms-ncep"
    assert newest["max_dbz"] == 42.5
    assert newest["key"].startswith("radar/colorado/202610160340-") and newest["key"].endswith(".tif")
    assert all(store.get(f["key"]) for f in frames)
    # One listing, then the files; AWS is never asked.
    assert not any("aws.test" in u for u, _ in mrms.requests)
    assert sum(u == f"{NCEP}/" for u, _ in mrms.requests) == 1


def test_nothing_to_fill_costs_no_requests(mrms):
    store = FakeStore(NOW)
    run_job(store, mrms)
    mrms.requests.clear()
    store.puts.clear()
    assert run_job(store, mrms) is True
    assert mrms.requests == [] and store.puts == []


def test_next_frame_is_added_and_the_oldest_dropped(mrms):
    store = FakeStore(NOW)
    run_job(store, mrms)
    mrms.put(datetime(2026, 10, 16, 3, 49, 50, tzinfo=UTC))
    later = NOW + timedelta(minutes=5)
    store.now = later
    assert run_job(store, mrms, later) is True
    times = [f["time"] for f in store.json(POINTER)["frames"]]
    assert times[-1] == "2026-10-16T03:50:00Z"
    assert times[0] == "2026-10-16T02:00:00Z" and len(times) == 12


def test_a_gap_is_backfilled_when_the_files_appear(mrms, bodies):
    gap = [file_for(FRAMES[3]), file_for(FRAMES[4])]
    for t in gap:
        del mrms.ncep[t], mrms.aws[t]
    mrms.rebuild()
    store = FakeStore(NOW)
    assert run_job(store, mrms) is True  # a gap isn't a failure; the frames just wait
    assert len(store.json(POINTER)["frames"]) == 10

    for t in gap:
        mrms.ncep[t] = bodies[t]
    mrms.rebuild()
    assert run_job(store, mrms) is True
    assert len(store.json(POINTER)["frames"]) == 12


def test_ncep_down_uses_aws_and_is_not_a_failure(mrms):
    mrms.down.add("ncep.test")
    store = FakeStore(NOW)
    assert run_job(store, mrms) is True
    frames = store.json(POINTER)["frames"]
    assert len(frames) == 12 and {f["source"] for f in frames} == {"mrms-aws"}


def test_both_down_keeps_frames_and_fails(mrms):
    store = FakeStore(NOW)
    run_job(store, mrms)
    before = store.json(POINTER)
    mrms.down.update({"ncep.test", "aws.test"})
    later = NOW + timedelta(minutes=5)
    assert run_job(store, mrms, later) is False
    # The 01:50 frame left the window; nothing else changed.
    assert store.json(POINTER)["frames"] == before["frames"][1:]


def test_bad_file_is_rejected_and_the_other_source_used(mrms):
    t = file_for(FRAMES[0])
    mrms.ncep[t] = b"<html>oops</html>"
    mrms.rebuild()
    store = FakeStore(NOW)
    assert run_job(store, mrms) is False  # the rejection shows in the cron log
    newest = store.json(POINTER)["frames"][-1]
    assert newest["time"] == "2026-10-16T03:40:00Z" and newest["source"] == "mrms-aws"


def test_old_frame_files_are_deleted(mrms):
    store = FakeStore(NOW)
    store.objects["radar/colorado/202610152300-deadbeef.tif"] = (b"x", NOW - timedelta(hours=4))
    store.objects["radar/colorado/202610160120-cafecafe.tif"] = (b"x", NOW - timedelta(hours=2, minutes=27))
    run_job(store, mrms)
    assert "radar/colorado/202610152300-deadbeef.tif" not in store.objects
    assert "radar/colorado/202610160120-cafecafe.tif" in store.objects


def test_dry_run_writes_nothing(mrms):
    store = FakeStore(NOW)

    async def go():
        async with mrms.client() as client:
            return await job.run(job.DryRunStore(store), client, ["colorado"], now=NOW, sources=SOURCES)

    assert asyncio.run(go()) is True
    assert store.objects == {}


# ── /tiles/radar ──────────────────────────────────────────────────────────────

def _ago(minutes: float) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now - timedelta(minutes=now.minute % 10) - timedelta(minutes=minutes)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _frame(t: datetime) -> dict:
    return {
        "time": _iso(t), "observed": _iso(t - timedelta(seconds=108)),
        "key": f"radar/colorado/{radar.frame_id(t)}-a1b2c3d4.tif",
        "source": "mrms-ncep", "fetched": _iso(t + timedelta(minutes=3)),
        "max_dbz": 42.5, "echo_share": 0.01,
    }


@pytest.fixture
def pointer(monkeypatch) -> dict:
    # The last 11 marks (newest at most ~12 min old whatever the clock says),
    # plus one long outside the two-hour window.
    frames = [_frame(_ago(150))] + [_frame(_ago(10 * i)) for i in range(10, -1, -1)]
    return fake_pointer(monkeypatch, radar, {"region": "colorado", "frames": frames})


@pytest.fixture
def titiler(monkeypatch) -> dict:
    return fake_titiler(monkeypatch)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


META = "/tiles/radar/meta?region=colorado"


def test_meta_lists_the_window_oldest_first(client, pointer):
    resp = client.get(META)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"
    body = resp.json()
    assert body["units"] == "dBZ" and body["attribution"] == "NOAA/NWS MRMS"
    assert body["interval_min"] == 10 and body["maxzoom"] == 9
    assert body["available"] is True and body["stale"] is False
    frames = body["frames"]
    assert len(frames) == 11
    newest = _ago(0)
    assert frames[-1]["time"] == body["latest"] == _iso(newest)
    assert frames[-1]["tiles"] == f"/tiles/radar/{radar.frame_id(newest)}/{{z}}/{{x}}/{{y}}?region=colorado"
    assert frames[0]["time"] < frames[-1]["time"]
    assert "key" not in frames[0]
    assert body["legend"][0] == {"min": 5, "max": 10, "color": "#04e9e7", "label": "5–10"}


def test_meta_stale_and_empty(client, pointer):
    pointer["value"]["frames"] = [_frame(_ago(30))]
    assert client.get(META).json()["stale"] is True
    pointer["value"] = None
    body = client.get(META).json()
    assert body["available"] is False and body["frames"] == [] and "stale" not in body


def test_legend_and_titiler_colormap_match():
    cmap = json.loads(radar.TITILER_COLORMAP)
    assert [tuple(rng) for rng, _ in cmap] == [(lo, hi) for lo, hi, _ in radar.SCALE]
    assert all(a[1] == b[0] for a, b in zip(radar.SCALE, radar.SCALE[1:], strict=False))


def test_frame_tile_is_cached_for_a_day(client, pointer, titiler):
    t = _ago(20)
    resp = client.get(f"/tiles/radar/{radar.frame_id(t)}/7/26/48?region=colorado")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=86400"
    params = titiler["calls"][0][1]
    assert params["url"].endswith(f"/dem-cogs/radar/colorado/{radar.frame_id(t)}-a1b2c3d4.tif")
    assert params["colormap"] == radar.TITILER_COLORMAP and params["nodata"] == "-999"
    client.get(f"/tiles/radar/{radar.frame_id(t)}/7/26/48?region=colorado")
    assert len(titiler["calls"]) == 1


def test_unknown_frame_is_a_short_cached_404(client, pointer, titiler):
    resp = client.get("/tiles/radar/202001010000/7/26/48?region=colorado")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "public, max-age=60"
    assert titiler["calls"] == []


@pytest.mark.parametrize(
    "url, code",
    [
        ("/tiles/radar/2026101603/7/26/48?region=colorado", 422),
        ("/tiles/radar/20261016034x/7/26/48?region=colorado", 422),
        ("/tiles/radar/202610160340/19/0/0?region=colorado", 400),
        ("/tiles/radar/202610160340/7/26/48?region=atlantis", 400),
    ],
)
def test_bad_tile_requests(client, pointer, titiler, url, code):
    assert client.get(url).status_code == code
    assert titiler["calls"] == []


def test_minio_down_is_503_no_store(client, pointer, titiler):
    pointer["value"] = httpx.ConnectError("minio down")
    for url in (META, f"/tiles/radar/{radar.frame_id(_ago(20))}/7/26/48?region=colorado"):
        resp = client.get(url)
        assert resp.status_code == 503 and resp.headers["cache-control"] == "no-store"


def test_radar_needs_a_tile_token_when_tokenless_is_off(client, pointer, titiler, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", False)
    assert client.get(META).status_code == 401
    token = tile_tokens.issue("i:install-0123456789abcdef", tile_tokens.TIER_ANON)[0]
    assert client.get(f"{META}&t={token}").status_code == 200


def test_status_radar(pointer):
    assert asyncio.run(status._check_radar()).healthy is True
    pointer["value"]["frames"] = [_frame(_ago(40))]
    health = asyncio.run(status._check_radar())
    assert health.healthy is False and "min old" in health.detail
    pointer["value"] = {"frames": []}
    assert asyncio.run(status._check_radar()).detail == "colorado: no data"
