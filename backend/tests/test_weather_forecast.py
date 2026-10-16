"""Spot forecast: GET /weather/forecast and app/services/nws.py. NWS and the
DEM are faked."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from limits import parse
from tenacity import wait_none

from app import http_retry, tile_tokens
from app.config import get_settings
from app.main import app
from app.routers import weather
from app.services import cog_sampler, nws

LAT, LON = 39.6, -105.9  # Loveland Pass area: BOU 31,58


def _token(tier: str = tile_tokens.TIER_ANON) -> str:
    subject = "i:install-0123456789abcdef" if tier == tile_tokens.TIER_ANON else "u:42"
    return tile_tokens.issue(subject, tier)[0]


def _url(lat: float = LAT, lon: float = LON, token: str | None = "anon") -> str:
    url = f"/weather/forecast?lat={lat}&lon={lon}"
    if token == "anon":
        token = _token()
    return f"{url}&t={token}" if token else url


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S+00:00")


H0 = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def gridpoints() -> dict:
    """A /gridpoints answer: hourly temperature (k °C at hour H0-1+k), one
    wind span for 3 days (16.09 km/h = 10 mph), no gusts, 6 h PoP and snow
    blocks (25.4 mm = 1 in each) starting 3 h before the current hour."""
    def spans(start, step_h, n, value):
        return [{"validTime": f"{_iso(start + timedelta(hours=step_h * k))}/PT{step_h}H",
                 "value": value(k)} for k in range(n)]
    return {"properties": {
        "updateTime": "2026-10-15T18:46:21+00:00",
        "elevation": {"unitCode": "wmoUnit:m", "value": 3001.0608},
        "temperature": {"uom": "wmoUnit:degC", "values": spans(H0 - timedelta(hours=1), 1, 52, lambda k: k)},
        "windSpeed": {"uom": "wmoUnit:km_h-1", "values": [
            {"validTime": f"{_iso(H0 - timedelta(hours=2))}/P3D", "value": 16.09344}]},
        "windGust": {"uom": "wmoUnit:km_h-1", "values": []},
        "windDirection": {"uom": "wmoUnit:degree_(angle)", "values": [
            {"validTime": f"{_iso(H0 - timedelta(hours=2))}/P2DT12H", "value": 300}]},
        "probabilityOfPrecipitation": {"uom": "wmoUnit:percent", "values": spans(H0 - timedelta(hours=3), 6, 10, lambda k: 40)},
        "snowfallAmount": {"uom": "wmoUnit:mm", "values": spans(H0 - timedelta(hours=3), 6, 10, lambda k: 25.4)},
    }}


def text_forecast() -> dict:
    now = datetime.now(UTC)
    names = ["Earlier", "Tonight", "Thursday", "Thursday Night", "Friday", "Friday Night"]
    periods = []
    for i, name in enumerate(names):
        start = now + timedelta(hours=12 * (i - 1) - 1)
        periods.append({
            "name": name, "startTime": _iso(start), "endTime": _iso(start + timedelta(hours=12)),
            "shortForecast": f"{name} snow", "detailedForecast": f"{name}: snow, 2 to 4 inches.",
        })
    return {"properties": {"periods": periods}}


class FakeNws:
    """routes[path] = (status, json) or an exception to raise."""

    def __init__(self) -> None:
        self.routes: dict[str, object] = {
            "/points/39.6000,-105.9000": (200, {"properties": {
                "gridId": "BOU", "gridX": 31, "gridY": 58,
                "forecastGridData": "https://api.weather.gov/gridpoints/BOU/31,58"}}),
            "/gridpoints/BOU/31,58": (200, gridpoints()),
            "/gridpoints/BOU/31,58/forecast": (200, text_forecast()),
        }
        self.calls: list[str] = []
        self.user_agents: set[str] = set()

    async def get(self, url, headers=None, **kwargs):
        path = url.removeprefix(nws.API)
        self.calls.append(path)
        self.user_agents.add((headers or {}).get("User-Agent"))
        route = self.routes.get(path, (404, {"title": "Not Found"}))
        if isinstance(route, Exception):
            raise route
        code, body = route
        return httpx.Response(code, json=body, request=httpx.Request("GET", url))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    monkeypatch.delitem(http_retry._BREAKERS, "nws", raising=False)
    monkeypatch.setattr(nws, "_points", type(nws._points)())
    monkeypatch.setattr(nws, "_forecasts", type(nws._forecasts)())
    monkeypatch.setattr(nws, "_locks", {})
    yield
    http_retry._BREAKERS.pop("nws", None)


@pytest.fixture
def fake(monkeypatch) -> FakeNws:
    f = FakeNws()
    monkeypatch.setattr(nws._HTTP, "get", f.get)
    return f


@pytest.fixture
def dem(monkeypatch) -> list:
    calls: list = []

    def sample(lon, lat, region, settings):
        calls.append((lon, lat, region))
        return 3780.0  # 12402 ft

    monkeypatch.setattr(cog_sampler, "sample_elevation", sample)
    return calls


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ── parsing ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value, hours",
    [("PT1H", 1), ("PT3H", 3), ("P1D", 24), ("P1DT6H", 30), ("P7DT13H", 181), ("PT30M", 0.5)],
)
def test_parse_interval(value, hours):
    start, end = nws.parse_interval(f"2026-10-15T12:00:00+00:00/{value}")
    assert start == datetime(2026, 10, 15, 12, tzinfo=UTC)
    assert (end - start).total_seconds() == hours * 3600


@pytest.mark.parametrize("value", ["2026-10-15T12:00:00+00:00/P", "2026-10-15T12:00:00+00:00/3H", "x/PT1H"])
def test_parse_interval_rejects_junk(value):
    with pytest.raises(ValueError):
        nws.parse_interval(value)


def test_unknown_units_are_refused():
    raw = gridpoints()
    raw["properties"]["windSpeed"]["uom"] = "wmoUnit:furlong_fortnight-1"
    with pytest.raises(ValueError, match="unexpected unit"):
        nws.parse_grid(raw)


def test_build_spreads_spans_over_48_hours():
    data = nws.parse_grid(gridpoints())
    data["periods"] = nws.parse_periods(text_forecast())
    now = H0 + timedelta(minutes=20)
    out = nws.build(data, nws.Grid("BOU", 31, 58), now)

    hours = out["hours"]
    assert len(hours) == 48
    assert hours[0]["time"] == nws.iso(H0) and hours[-1]["time"] == nws.iso(H0 + timedelta(hours=47))
    assert hours[0] == {"time": nws.iso(H0), "temp_f": 34, "wind_mph": 10, "gust_mph": None,
                        "wind_dir_deg": 300, "pop_pct": 40}
    assert hours[10]["temp_f"] == 52  # 11 °C
    assert hours[47]["wind_dir_deg"] == 300  # one 2.5-day span from H0-2 covers all 48 h
    # Snow stays in NWS's 6 h blocks; only the ones overlapping the 48 h.
    assert len(out["snow"]) == 9
    assert out["snow"][0] == {"start": nws.iso(H0 - timedelta(hours=3)), "end": nws.iso(H0 + timedelta(hours=3)), "snow_in": 1.0}
    assert [p["name"] for p in out["periods"]] == ["Tonight", "Thursday", "Thursday Night", "Friday"]
    assert out["grid"] == {"office": "BOU", "x": 31, "y": 58, "elevation_ft": 9846}
    assert out["units"] == {"temp": "F", "wind": "mph", "snow": "in", "pop": "%"}
    assert out["attribution"] == "NOAA/National Weather Service"


# ── GET /weather/forecast ─────────────────────────────────────────────────────

def test_forecast_for_a_tap(client, fake, dem):
    resp = client.get(_url())
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, max-age=300"
    body = resp.json()
    assert (body["lat"], body["lon"]) == (LAT, LON)
    assert body["grid"]["office"] == "BOU" and body["grid"]["elevation_ft"] == 9846
    assert body["point_elevation_ft"] == 12402
    assert dem == [(LON, LAT, "colorado")]
    assert body["stale"] is False and body["updated"] == "2026-10-15T18:46:21+00:00"
    assert len(body["hours"]) == 48 and len(body["periods"]) == 4
    assert body["periods"][0]["detailed"] == "Tonight: snow, 2 to 4 inches."
    assert fake.user_agents == {get_settings().weather_user_agent}


def test_one_grid_cell_is_fetched_once(client, fake, dem):
    client.get(_url())
    client.get(_url(39.6004, -105.9003))  # rounds to the same 3 decimals
    assert fake.calls.count("/points/39.6000,-105.9000") == 1
    assert fake.calls.count("/gridpoints/BOU/31,58") == 1

    # Another tap in the same NWS cell: a new /points lookup, same forecast.
    fake.routes["/points/39.6100,-105.9100"] = fake.routes["/points/39.6000,-105.9000"]
    client.get(_url(39.61, -105.91))
    assert fake.calls.count("/gridpoints/BOU/31,58") == 1


def test_forecast_is_refetched_after_45_minutes(client, fake, dem):
    client.get(_url())
    grid = nws.Grid("BOU", 31, 58)
    data, fetched = nws._forecasts[grid]
    nws._forecasts[grid] = (data, fetched - timedelta(minutes=46))
    client.get(_url())
    assert fake.calls.count("/gridpoints/BOU/31,58") == 2


def test_nws_down_serves_a_recent_cached_forecast_as_stale(client, fake, dem):
    client.get(_url())
    grid = nws.Grid("BOU", 31, 58)
    data, fetched = nws._forecasts[grid]
    nws._forecasts[grid] = (data, fetched - timedelta(hours=2))
    fake.routes["/gridpoints/BOU/31,58"] = (503, {})
    body = client.get(_url()).json()
    assert body["stale"] is True
    assert body["fetched"] == nws.iso(fetched - timedelta(hours=2))

    nws._forecasts[grid] = (data, fetched - timedelta(hours=13))
    resp = client.get(_url())
    assert resp.status_code == 502 and resp.headers["cache-control"] == "no-store"


def test_nws_down_with_nothing_cached_is_502(client, fake, dem):
    fake.routes["/gridpoints/BOU/31,58"] = httpx.ConnectError("down")
    assert client.get(_url()).status_code == 502
    fake.routes["/points/39.7000,-105.8000"] = (500, {})
    assert client.get(_url(39.7, -105.8)).status_code == 502  # /points fails too


def test_text_forecast_failing_still_serves_the_hours(client, fake, dem):
    fake.routes["/gridpoints/BOU/31,58/forecast"] = (500, {"title": "Unexpected Problem"})
    resp = client.get(_url())
    assert resp.status_code == 200
    body = resp.json()
    assert body["periods"] is None and len(body["hours"]) == 48


def test_unexpected_units_are_502(client, fake, dem):
    raw = gridpoints()
    raw["properties"]["temperature"]["uom"] = "wmoUnit:K"
    fake.routes["/gridpoints/BOU/31,58"] = (200, raw)
    assert client.get(_url()).status_code == 502


def test_point_outside_nws_is_404_and_remembered(client, fake, dem):
    resp = client.get(_url(51.5, -0.1))
    assert resp.status_code == 404
    assert client.get(_url(51.5, -0.1)).status_code == 404
    assert fake.calls.count("/points/51.5000,-0.1000") == 1


def test_point_outside_our_regions_has_no_dem_elevation(client, fake, dem):
    fake.routes["/points/44.4300,-110.5900"] = fake.routes["/points/39.6000,-105.9000"]
    body = client.get(_url(44.43, -110.59)).json()  # Yellowstone
    assert body["point_elevation_ft"] is None and dem == []


def test_dem_failure_does_not_fail_the_forecast(client, fake, monkeypatch):
    def broken(*args):
        raise RuntimeError("vsicurl timeout")

    monkeypatch.setattr(cog_sampler, "sample_elevation", broken)
    resp = client.get(_url())
    assert resp.status_code == 200 and resp.json()["point_elevation_ft"] is None


@pytest.mark.parametrize("query", ["lat=91&lon=0", "lat=0&lon=-181", "lat=abc&lon=0", "lon=0"])
def test_bad_coordinates_are_422(client, fake, query):
    assert client.get(f"/weather/forecast?{query}&t={_token()}").status_code == 422
    assert fake.calls == []


# ── access + rate limit ───────────────────────────────────────────────────────

def test_needs_a_valid_tile_token_even_when_tokenless_tiles_are_on(client, fake, dem, monkeypatch):
    monkeypatch.setattr(get_settings(), "tiles_allow_tokenless", True)
    for url in (_url(token=None), _url(token="junk.token")):
        resp = client.get(url)
        assert resp.status_code == 401 and resp.headers["cache-control"] == "no-store"
    expired = tile_tokens._sign({"v": 1, "sub": "i:install-0123456789abcdef", "tier": "anon", "exp": 1})
    assert client.get(_url(token=expired)).status_code == 401
    assert fake.calls == []


def test_signed_in_and_anonymous_tokens_both_work(client, fake, dem):
    assert client.get(_url(token=_token(tile_tokens.TIER_USER))).status_code == 200
    assert client.get(_url(token=_token(tile_tokens.TIER_ANON))).status_code == 200


def test_rate_limited_per_ip(client, fake, dem, monkeypatch):
    monkeypatch.setitem(tile_tokens.BUCKETS, "weather", (parse("3/minute"),))
    for _ in range(3):
        assert client.get(_url()).status_code == 200
    resp = client.get(_url())
    assert resp.status_code == 429
    assert resp.headers["cache-control"] == "no-store"
    assert int(resp.headers["retry-after"]) >= 1
    other = client.get(_url(), headers={"x-forwarded-for": "203.0.113.9"})
    assert other.status_code == 200



def test_region_at():
    assert weather._region_at(-105.9, 39.6) == "colorado"
    assert weather._region_at(-0.1, 51.5) is None
