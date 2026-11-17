"""Recent Sentinel-2 (app/services/sentinel.py, app/jobs/sentinel_refresh.py):
the newest clear view wins per pixel whatever order scenes come in, clouds
never replace a view, a scene with nothing new is not read past its SCL,
views older than the window are empty in the COGs, and a run publishes a
revision only when enough of the grid has a view. Earth Search is faked;
scenes are small local GeoTIFFs in UTM 13N."""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from app.jobs import sentinel_refresh as job
from app.services import sentinel
from app.services.sentinel import Grid
from tests.weather_fakes import FakeStore, reset_breakers

BOX = (-105.80, 39.78, -105.76, 39.81)  # ~3.4 x 3.3 km near Berthoud Pass
NOW = datetime(2026, 11, 17, 9, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fresh_breakers(monkeypatch):
    reset_breakers(monkeypatch, "earth_search", "minio")


@pytest.fixture(autouse=True)
def local_files(monkeypatch):
    """Scenes in tests are local files, not sentinel-cogs URLs."""
    monkeypatch.setattr(sentinel, "HREF_PREFIX", "")


# ── pixels ───────────────────────────────────────────────────────────────────

def test_stretch_keeps_snow_texture():
    got = sentinel.stretch(np.array([0, 500, 2000, 7000, 9000, 10000, 15000], dtype=np.uint16))
    assert got[0] == 0 and got[-2] == 255 and got[-1] == 255
    assert got[3] < got[4] < 250  # 0.7 and 0.9 reflectance stay apart (TCI would clip both)
    assert 60 < got[1] < 80       # dark forest is not black


def test_false_colour_is_swir_nir_red():
    bands = {name: np.full((1, 1), v, dtype=np.uint16)
             for name, v in (("red", 9000), ("green", 9000), ("blue", 9000), ("nir08", 8000), ("swir16", 500))}
    out = sentinel.render(bands)
    r, g, b = out["false"][:, 0, 0]
    assert r < 100 and g > 200 and b > 200  # snow: low SWIR, so cyan
    assert out["true"].shape == (3, 1, 1)


@pytest.mark.parametrize("dates, credit", [
    (["2026-10-20", "2026-11-16"], "Contains modified Copernicus Sentinel data 2026"),
    (["2025-12-20", "2026-01-10"], "Contains modified Copernicus Sentinel data 2025-2026"),
    (None, "Contains modified Copernicus Sentinel data 2026"),
])
def test_attribution(dates, credit):
    assert sentinel.attribution(dates, today=date(2026, 11, 17)) == credit


def test_grid_and_windows():
    grid = Grid(BOX)
    assert grid.res == 12.5 and grid.x0 % 12.5 == 0 and grid.y1 % 12.5 == 0
    # ~3.4 x 3.3 km on the ground is ~4.4 x 4.3 km in Web Mercator at 39.8°N.
    assert 340 < grid.width < 370 and 330 < grid.height < 360
    assert grid.window_of(BOX) == (0, 0, grid.width, grid.height)
    assert grid.window_of((-100.0, 30.0, -99.9, 30.1)) is None


def test_day_numbers_round_trip():
    assert sentinel.from_day_number(sentinel.day_number(date(2026, 11, 14))) == date(2026, 11, 14)
    assert sentinel.day_number(sentinel.EPOCH) == 0


# ── scenes ───────────────────────────────────────────────────────────────────

def _utm_box(pad_m: float = 2000.0) -> tuple[float, float, float, float]:
    west, south, east, north = transform_bounds("EPSG:4326", "EPSG:26913", *BOX)
    return west - pad_m, south - pad_m, east + pad_m, north + pad_m


def _write(path: Path, data: np.ndarray, res: float) -> str:
    west, _, _, north = _utm_box()
    with rasterio.open(path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                       dtype=data.dtype, crs="EPSG:26913", transform=from_origin(west, north, res, res)) as dst:
        dst.write(data, 1)
    return str(path)


def make_scene(tmp_path: Path, name: str, day: date, *, scl_left: int = 4, scl_right: int = 4,
               value: int = 3000, bands: bool = True) -> job.Scene:
    """A scene over the whole box: SCL classes for the west and east halves,
    every band `value`. bands=False points the bands at missing files, so
    reading them would fail."""
    west, south, east, north = _utm_box()
    w20, h20 = int((east - west) / 20), int((north - south) / 20)
    scl = np.full((h20, w20), scl_right, dtype=np.uint8)
    scl[:, : w20 // 2] = scl_left
    assets = {"scl": _write(tmp_path / f"{name}-scl.tif", scl, 20)}
    for band in job.BANDS:
        res = 10 if band in ("red", "green", "blue") else 20
        w, h = int((east - west) / res), int((north - south) / res)
        path = tmp_path / f"{name}-{band}.tif"
        assets[band] = _write(path, np.full((h, w), value, dtype=np.uint16), res) if bands else str(path)
    return job.Scene(name, datetime.combine(day, datetime.min.time(), UTC), BOX, 10.0, assets)


@pytest.fixture
def mosaic(tmp_path):
    m = job.Mosaic(tmp_path / "work" / "colorado", Grid(BOX))
    yield m
    m.close()


def _days(m: job.Mosaic) -> np.ndarray:
    return m.ds["date"].read(1)


def test_newest_clear_view_wins_in_any_order(tmp_path, mosaic):
    old = make_scene(tmp_path, "old", date(2026, 11, 1), value=1000)
    new = make_scene(tmp_path, "new", date(2026, 11, 14), scl_left=9, scl_right=11, value=8000)  # cloud | snow
    # The newer scene first, then the older one arriving late.
    assert job.apply_scene(mosaic, new) > 0
    assert job.apply_scene(mosaic, old) > 0
    days = _days(mosaic)
    mid = mosaic.grid.width // 2
    assert (days[:, : mid - 40] == sentinel.day_number(date(2026, 11, 1))).all()   # under the cloud
    assert (days[:, mid + 40:] == sentinel.day_number(date(2026, 11, 14))).all()   # clear snow
    true = mosaic.ds["true"].read()
    assert true[0, 10, 10] < true[0, 10, -10]  # dark old view west, bright snow east


def test_a_scene_with_nothing_newer_is_read_no_further_than_scl(tmp_path, mosaic):
    job.apply_scene(mosaic, make_scene(tmp_path, "new", date(2026, 11, 14)))
    stale = make_scene(tmp_path, "stale", date(2026, 11, 2), bands=False)  # its bands don't exist
    assert job.apply_scene(mosaic, stale) == 0


def test_clouds_shadows_and_no_data_never_take_a_pixel(tmp_path, mosaic):
    for i, cls in enumerate((0, 1, 3, 8, 9, 10)):
        scene = make_scene(tmp_path, f"c{cls}", date(2026, 11, 10 + i), scl_left=cls, scl_right=cls, bands=False)
        assert job.apply_scene(mosaic, scene) == 0
    assert _days(mosaic).max() == 0


def test_export_empties_views_older_than_the_window(tmp_path, mosaic):
    job.apply_scene(mosaic, make_scene(tmp_path, "old", date(2026, 10, 1), scl_right=9))  # west only
    job.apply_scene(mosaic, make_scene(tmp_path, "new", date(2026, 11, 14), scl_left=9))  # east only
    out = job.export(mosaic, tmp_path / "out", cutoff=date(2026, 10, 18), bbox=BOX)
    assert 0.4 < out.coverage < 0.6 and out.dates == ["2026-11-14", "2026-11-14"]
    with rasterio.open(out.paths["true"]) as cog:
        mask = cog.dataset_mask()
        assert cog.profile["compress"].lower() == "jpeg"
        assert mask[:, :20].max() == 0 and mask[:, -20:].min() == 255
    with rasterio.open(out.paths["date"]) as cog:
        days = cog.read(1)
        assert cog.nodata == 0 and days[:, :20].max() == 0
        assert (days[:, -20:] == sentinel.day_number(date(2026, 11, 14))).all()
    doc = json.loads(gzip.decompress(out.paths["dates"].read_bytes()))
    assert doc["dates"] == ["2026-11-14"] and doc["cell_deg"] == 0.01
    assert sentinel.date_at(doc, -105.762, 39.795) == "2026-11-14"   # east
    assert sentinel.date_at(doc, -105.798, 39.795) is None           # west: too old
    assert sentinel.date_at(doc, -100.0, 39.795) is None              # off the grid


def test_dates_doc_round_trips():
    d1, d2 = sentinel.day_number(date(2026, 11, 6)), sentinel.day_number(date(2026, 11, 16))
    days = np.array([[0, d1], [d2, d2]], dtype=np.uint16)
    doc = sentinel.dates_doc(days, west=-106.0, north=40.0, cell=0.5)
    assert doc["dates"] == ["2026-11-06", "2026-11-16"] and (doc["width"], doc["height"]) == (2, 2)
    assert sentinel.date_at(doc, -105.9, 39.9) is None
    assert sentinel.date_at(doc, -105.4, 39.9) == "2026-11-06"
    assert sentinel.date_at(doc, -105.9, 39.4) == "2026-11-16"


def test_a_new_stretch_rebuilds_the_mosaic(tmp_path, monkeypatch):
    path = tmp_path / "work" / "colorado"
    m = job.Mosaic(path, Grid(BOX))
    job.apply_scene(m, make_scene(tmp_path, "s", date(2026, 11, 14)))
    m.done["s"] = "2026-11-14"
    m.save(date(2026, 10, 1))
    m.close()

    m = job.Mosaic(path, Grid(BOX))  # same grid and stretch: kept
    assert m.done == {"s": "2026-11-14"} and _days(m).max() > 0
    m.close()

    monkeypatch.setattr(sentinel, "RENDER", "g24-v2")
    m = job.Mosaic(path, Grid(BOX))
    assert m.done == {} and _days(m).max() == 0
    m.close()


# ── a run ────────────────────────────────────────────────────────────────────

def _feature(scene: job.Scene, cloud: float = 10.0) -> dict:
    return {"id": scene.id, "bbox": list(BOX),
            "properties": {"datetime": scene.when.isoformat().replace("+00:00", "Z"), "eo:cloud_cover": cloud},
            "assets": {name: {"href": href} for name, href in scene.assets.items()}}


class FakeEarthSearch:
    def __init__(self, features: list[dict]) -> None:
        self.features = features
        self.bodies: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        if body.get("next") == "2":
            return httpx.Response(200, json={"features": self.features[1:], "links": []})
        return httpx.Response(200, json={"features": self.features[:1], "links": [
            {"rel": "next", "href": str(request.url), "body": dict(body, next="2")}]})


def _run(tmp_path, store, features, now=NOW, monkeypatch=None):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(FakeEarthSearch(features))) as client:
            return await job.run(store, client, ["colorado"], now, work_dir=tmp_path / "work",
                                 search_url="https://earth-search.test/v1/search")
    return asyncio.run(go())


@pytest.fixture
def small_region(monkeypatch):
    """Colorado's grid shrunk to BOX, so a run is quick."""
    from app.regions import REGIONS
    monkeypatch.setattr(REGIONS["colorado"], "bbox", BOX)


def test_run_publishes_three_cogs_and_a_pointer(tmp_path, small_region):
    store = FakeStore(NOW)
    scenes = [make_scene(tmp_path, "S2B_13TDE_20261116_0_L2A", date(2026, 11, 16), scl_left=9),
              make_scene(tmp_path, "S2C_13TDE_20261111_0_L2A", date(2026, 11, 11)),
              make_scene(tmp_path, "S2A_13TDE_20261110_0_L2A", date(2026, 11, 10), bands=False)]
    features = [_feature(scenes[0]), _feature(scenes[1]), _feature(scenes[2], cloud=100.0)]
    assert _run(tmp_path, store, features) is True
    entry = store.json(sentinel.pointer_key("colorado"))["current"]
    assert entry["revision"] == "20261117-0900" and entry["window_days"] == 30
    assert entry["dates"] == ["2026-11-11", "2026-11-16"] and entry["coverage"] == 1.0
    assert entry["newest_scene"] == "2026-11-16T00:00:00Z" and entry["render"] == sentinel.RENDER
    assert set(entry["keys"]) == {"true", "false", "date", "dates"} and entry["key"] == entry["keys"]["true"]
    for name in ("true", "false", "date"):
        assert store.objects[entry["keys"][name]][0][:2] in (b"II", b"MM")  # a TIFF
    doc = json.loads(gzip.decompress(store.objects[entry["keys"]["dates"]][0]))
    assert doc["dates"] == ["2026-11-11", "2026-11-16"]
    state = json.loads((tmp_path / "work" / "colorado" / "state.json").read_text())
    assert set(state["done"]) == {s.id for s in scenes}  # the 100% cloud one is skipped, not retried


def test_run_with_too_little_view_publishes_nothing(tmp_path, small_region):
    store = FakeStore(NOW)
    cloudy = make_scene(tmp_path, "S2B_13TDE_20261116_0_L2A", date(2026, 11, 16), scl_left=9, scl_right=9)
    assert _run(tmp_path, store, [_feature(cloudy)]) is False
    assert sentinel.pointer_key("colorado") not in store.objects


def test_run_skips_scenes_outside_sentinel_cogs(tmp_path, small_region, monkeypatch):
    monkeypatch.setattr(sentinel, "HREF_PREFIX", "https://sentinel-cogs.s3.us-west-2.amazonaws.com/")
    feature = _feature(make_scene(tmp_path, "x", date(2026, 11, 16)))
    assert job.parse_scene(feature) is None
