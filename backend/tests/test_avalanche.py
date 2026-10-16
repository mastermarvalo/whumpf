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
