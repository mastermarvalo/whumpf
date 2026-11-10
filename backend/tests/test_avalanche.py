"""/avalanche/observations: fetched_at marks when the cache was filled from CAIC."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app import http_retry
from app.routers import avalanche


class _Resp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> list:
        return [{"report_id": "a1", "latitude": 39.6, "longitude": -106.0}]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(avalanche, "_obs_cache", None)
    monkeypatch.delitem(http_retry._BREAKERS, "caic-obs", raising=False)
    yield
    http_retry._BREAKERS.pop("caic-obs", None)


def test_fetched_at_set_and_kept_when_refresh_fails(monkeypatch):
    async def ok(*a, **k):
        return _Resp()

    monkeypatch.setattr(avalanche._HTTP, "get", ok)
    first = asyncio.run(avalanche.get_observations())
    stamp = datetime.fromisoformat(first["fetched_at"])
    assert stamp.utcoffset() == timedelta(0)
    assert len(first["features"]) == 1

    # Expire the cache, then make CAIC fail: stale data must keep its stamp.
    data, fetched = avalanche._obs_cache
    monkeypatch.setattr(avalanche, "_obs_cache", (data, fetched - timedelta(hours=1)))

    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    async def run_stale():
        monkeypatch.setattr(avalanche._HTTP, "get", boom)
        monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: __import__("tenacity").wait_none())
        served = await avalanche.get_observations()
        await avalanche._bg_refresh_obs()
        return served, await avalanche.get_observations()

    served, after = asyncio.run(run_stale())
    assert served["fetched_at"] == first["fetched_at"]
    assert after["fetched_at"] == first["fetched_at"]


# ── zone names: AVID area ids → CAIC named backcountry zones ─────────────────

_AREA_ID = "a141d5b6"
_SQUARE = {  # lng -106.5..-105.5, lat 39..40
    "type": "MultiPolygon",
    "coordinates": [[[[-106.5, 39.0], [-105.5, 39.0], [-105.5, 40.0], [-106.5, 40.0], [-106.5, 39.0]]]],
}
_PRODUCTS = [{
    "type": "avalancheforecast", "areaId": _AREA_ID, "forecaster": "CAIC",
    "issueDateTime": "2026-01-10T00:00:00Z",
    "dangerRatings": {"days": [{"alp": "considerable", "tln": "moderate", "btl": "low"}]},
    "avalancheProblems": {"days": [[]]},
}]
_AREAS = {"features": [{
    "geometry": {"type": "MultiPolygon", "coordinates": [[[[-110, 36], [-100, 36], [-100, 42], [-110, 42], [-110, 36]]]]},
    "properties": {"id": _AREA_ID, "centroid": [-106.0, 39.5]},
}]}
_ZONES = [
    {"category": "backcountry", "is_leaf": True, "title": "Vail & Summit County",
     "geojson_url": "https://caic.test/vail.geojson"},
    {"category": "statewide-backcountry", "is_leaf": False, "title": "Statewide",
     "geojson_url": "https://caic.test/statewide.geojson"},
]
_VAIL_GEOJSON = {"type": "FeatureCollection", "features": [{
    "type": "Feature", "geometry": _SQUARE,
    "properties": {"name": "Vail & Summit County", "category": "backcountry"},
}]}


class _JsonResp:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload, self._status = payload, status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("GET", "x"),
                                        response=httpx.Response(self._status))

    def json(self):
        return self._payload


@pytest.fixture
def _zones_env(monkeypatch):
    monkeypatch.setattr(avalanche, "_zones_cache", None)
    monkeypatch.setattr(avalanche, "_avid_cache", ((_PRODUCTS, _AREAS), datetime.now(UTC)))
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: __import__("tenacity").wait_none())
    monkeypatch.delitem(http_retry._BREAKERS, "caic-zones", raising=False)
    requested: list[str] = []

    async def ok(url, *a, **k):
        requested.append(url)
        if url == avalanche._ZONES_URL:
            return _JsonResp(_ZONES)
        if url == "https://caic.test/vail.geojson":
            return _JsonResp(_VAIL_GEOJSON)
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(avalanche._HTTP, "get", ok)
    yield requested
    http_retry._BREAKERS.pop("caic-zones", None)


def test_zone_detail_uses_named_zone(_zones_env):
    detail = asyncio.run(avalanche.get_zone_detail_for_point(39.6, -106.0))
    assert detail["zone"] == "Vail & Summit County"
    assert detail["zone_id"] == _AREA_ID
    assert detail["danger"]["alp"] == "considerable"
    # Non-leaf / non-backcountry entries are never fetched.
    assert "https://caic.test/statewide.geojson" not in _zones_env


def test_point_outside_all_zones_falls_back_to_id(_zones_env):
    detail = asyncio.run(avalanche.get_zone_detail_for_point(37.5, -108.0))
    assert detail["zone"] == _AREA_ID
    assert detail["zone_id"] == _AREA_ID


def test_zones_fetch_failure_without_cache_falls_back_to_id(_zones_env, monkeypatch):
    async def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(avalanche._HTTP, "get", boom)
    detail = asyncio.run(avalanche.get_zone_detail_for_point(39.6, -106.0))
    assert detail["zone"] == _AREA_ID
    assert detail["zone_id"] == _AREA_ID
    assert avalanche._zones_cache is None


def test_zones_refresh_failure_keeps_last_good_copy(_zones_env, monkeypatch):
    asyncio.run(avalanche.get_zones())
    zones, fetched = avalanche._zones_cache
    monkeypatch.setattr(avalanche, "_zones_cache", (zones, fetched - timedelta(hours=25)))

    async def geojson_500(url, *a, **k):
        if url == avalanche._ZONES_URL:
            return _JsonResp(_ZONES)
        return _JsonResp({}, status=500)

    monkeypatch.setattr(avalanche._HTTP, "get", geojson_500)

    async def run_stale():
        await avalanche._bg_refresh_zones()
        return await avalanche.get_zone_detail_for_point(39.6, -106.0)

    detail = asyncio.run(run_stale())
    assert detail["zone"] == "Vail & Summit County"
    assert avalanche._zones_cache[0] is zones


def test_zone_detail_endpoint_returns_name_and_id(_zones_env):
    from fastapi.testclient import TestClient

    from app.main import app

    resp = TestClient(app).get("/avalanche/zone_detail?lat=39.6&lng=-106.0")
    assert resp.status_code == 200, resp.text
    assert resp.json()["zone"] == "Vail & Summit County"
    assert resp.json()["zone_id"] == _AREA_ID


def test_avalanche_is_public_and_rate_limited_per_ip(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.tile_tokens import BUCKETS

    monkeypatch.setattr(avalanche, "_forecast_cache", ({"features": []}, datetime.now(UTC)))
    monkeypatch.setitem(BUCKETS, "avalanche", (__import__("limits").parse("3/minute"),))
    client = TestClient(app)  # no auth override: signed-out
    for _ in range(3):
        assert client.get("/avalanche/forecast").status_code == 200
    resp = client.get("/avalanche/forecast")
    assert resp.status_code == 429
    assert resp.headers["cache-control"] == "no-store"
    assert int(resp.headers["retry-after"]) >= 1
    # A different client IP has its own counter.
    other = client.get("/avalanche/forecast", headers={"x-forwarded-for": "203.0.113.9"})
    assert other.status_code == 200


# ── cache timestamps are timezone-aware UTC (naive/aware mixes raise TypeError) ──

def test_cache_timestamps_are_aware_and_fresh_check_works(monkeypatch):
    class _ForecastResp(_Resp):
        def json(self) -> dict:
            return {"features": []}

    async def ok(*a, **k):
        is_forecast = k.get("params", {}).get("type") == "forecast"
        return _ForecastResp() if is_forecast else _Resp()

    monkeypatch.setattr(avalanche._HTTP, "get", ok)
    monkeypatch.setattr(avalanche, "_forecast_cache", None)
    monkeypatch.setattr(avalanche, "_obs_cache", None)
    monkeypatch.delitem(http_retry._BREAKERS, "caic-forecast", raising=False)

    async def run():
        await avalanche.get_forecast()
        await avalanche.get_observations()
        # second calls hit the freshness comparison against the stored stamp
        await avalanche.get_forecast()
        await avalanche.get_observations()

    asyncio.run(run())
    for cache in (avalanche._forecast_cache, avalanche._obs_cache):
        assert cache is not None
        assert cache[1].tzinfo is not None
        assert cache[1].utcoffset() == timedelta(0)


def test_no_naive_utcnow_in_app_code():
    """utcnow() is naive; mixing it with aware stamps raises TypeError at compare time."""
    from pathlib import Path

    root = Path(avalanche.__file__).resolve().parents[1]
    offenders = [str(p) for p in root.rglob("*.py") if "utcnow(" in p.read_text()]
    assert offenders == []


# ── AVID fetch against a real CAIC response ───────────────────────────────────
# tests/fixtures/caic_avid_2026-11-09.json is CAIC's own /avid/products/all and
# /avid/products/all/area output from 2026-11-09 (one forecast, polygon list cut
# to 3). In Nov 2026 CAIC's new site dropped /api-proxy/avid (404) for /avid/*.

def test_fetch_avid_uses_current_caic_paths_and_parses_real_response(monkeypatch):
    import json
    from pathlib import Path

    fx = json.loads((Path(__file__).parent / "fixtures" / "caic_avid_2026-11-09.json").read_text())
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/avid/products/all":
            return httpx.Response(200, json=fx["products"] + [{"type": "regionaldiscussion"}])
        if request.url.path == "/avid/products/all/area":
            return httpx.Response(200, json=fx["areas"])
        return httpx.Response(404, text="<html>not found</html>")

    monkeypatch.setattr(avalanche, "_HTTP", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(avalanche, "_avid_cache", None)
    monkeypatch.delitem(http_retry._BREAKERS, "caic-avid", raising=False)
    monkeypatch.setattr(avalanche, "_zones_cache", None)

    products, areas = asyncio.run(avalanche._do_fetch_avid())

    assert sorted(seen) == ["/avid/products/all", "/avid/products/all/area"]
    assert [p["type"] for p in products] == ["avalancheforecast"]  # other types filtered out
    detail = avalanche._build_zone_detail(products[0])
    assert detail["danger"] == {"alp": "low", "tln": "low", "btl": "low"}
    assert detail["valid_date"] == "2026-11-09"
    assert products[0]["areaId"] == areas["features"][0]["properties"]["id"]
    http_retry._BREAKERS.pop("caic-avid", None)


# ── avalanche problems: parsing and the "unreadable" guard ────────────────────

def _fixture(name: str) -> dict:
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / name).read_text())


def _synthetic_product() -> dict:
    return _fixture("caic_avid_SYNTHETIC_problems.json")["products"][0]


def test_problems_parse_synthetic_fixture():
    """SYNTHETIC fixture (hand-built in the documented AVID shape, not a real
    CAIC response): checks the parsing logic, not CAIC's real format."""
    detail = avalanche._build_zone_detail(_synthetic_product())
    assert detail["problems_status"] == "ok"
    wind, persistent = detail["problems"]
    assert (wind["type"], wind["label"], wind["likelihood"]) == ("windSlab", "Wind Slab", "Likely")
    assert (wind["size_min"], wind["size_max"]) == ("1", "2")
    assert wind["aspects"] == ["N", "NE", "E"]
    assert wind["elevations"] == ["alp", "tln"]
    assert wind["aspect_elevations"] == ["n_alp", "ne_alp", "e_alp", "n_tln", "ne_tln"]
    assert persistent["label"] == "Persistent Slab" and persistent["likelihood"] == "Possible"
    assert persistent["aspects"] == ["N", "NE", "NW"]
    assert persistent["elevations"] == ["tln", "btl"]


def test_real_no_problems_forecast_is_ok_and_empty():
    detail = avalanche._build_zone_detail(_fixture("caic_avid_2026-11-09.json")["products"][0])
    assert detail["problems"] == [] and detail["problems_status"] == "ok"


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("avalancheProblems"),
    lambda p: p.update(avalancheProblems=None),
    lambda p: p.update(avalancheProblems={"days": []}),
    lambda p: p.update(avalancheProblems={"days": "none"}),
    lambda p: p.update(avalancheProblems={"days": [None, []]}),
    lambda p: p.update(avalancheProblems={"rows": [[]]}),
    lambda p: p["avalancheProblems"]["days"][0][0].pop("type"),
    lambda p: p["avalancheProblems"]["days"][0][0].pop("aspectElevations"),
    lambda p: p["avalancheProblems"]["days"][0][0].update(aspectElevations="n_alp"),
    lambda p: p["avalancheProblems"]["days"][0].append("windSlab"),
], ids=["missing", "null", "no-days", "days-not-list", "day0-not-list", "other-key",
        "no-type", "no-aspectElevations", "aspectElevations-not-list", "problem-not-object"])
def test_unreadable_problems_are_flagged_not_empty(mutate, caplog):
    product = _synthetic_product()
    mutate(product)
    with caplog.at_level("WARNING", logger="whumpf.avalanche"):
        detail = avalanche._build_zone_detail(product)
    assert detail["problems_status"] == "unreadable"
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_unreadable_problem_keeps_the_readable_ones():
    product = _synthetic_product()
    product["avalancheProblems"]["days"][0][0].pop("type")
    detail = avalanche._build_zone_detail(product)
    assert detail["problems_status"] == "unreadable"
    assert [p["type"] for p in detail["problems"]] == ["persistentSlab"]


def test_null_expected_size_does_not_crash():
    product = _synthetic_product()
    product["avalancheProblems"]["days"][0][0]["expectedSize"] = None
    detail = avalanche._build_zone_detail(product)
    assert detail["problems_status"] == "ok" and detail["problems"][0]["size_min"] == ""


def test_zone_detail_endpoint_returns_problems_status(_zones_env):
    from fastapi.testclient import TestClient
    from app.main import app

    body = TestClient(app).get("/avalanche/zone_detail", params={"lat": 39.6, "lng": -106.0}).json()
    assert body["problems_status"] == "ok" and body["problems"] == []


# ── avid_capture job ──────────────────────────────────────────────────────────

def test_capture_picks_first_forecast_with_problems_and_trims():
    from app.jobs import avid_capture

    syn = _fixture("caic_avid_SYNTHETIC_problems.json")
    empty = _fixture("caic_avid_2026-11-09.json")["products"][0]
    found = avid_capture.pick([empty, syn["products"][0]], syn["areas"])
    assert found["products"][0]["avalancheProblems"]["days"][0]
    assert len(found["products"][0]["polygons"]) <= 3
    assert found["areas"]["features"][0]["properties"]["id"] == found["products"][0]["areaId"]


def test_capture_returns_none_without_problems():
    from app.jobs import avid_capture

    real = _fixture("caic_avid_2026-11-09.json")
    assert avid_capture.pick(real["products"], real["areas"]) is None
