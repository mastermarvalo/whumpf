"""Spot forecast from api.weather.gov (NOAA/NWS), for GET /weather/forecast.

Three NWS calls per forecast grid cell (2.5 km):

    /points/{lat},{lon}               -> office + grid x/y       cached 7 days
    /gridpoints/{office}/{x},{y}      -> hourly values (raw)     cached 45 min
    /gridpoints/{office}/{x},{y}/forecast -> text periods        with the above

Taps are rounded to 3 decimals (~100 m) for the grid lookup, and every tap
in the same cell shares one cached forecast. When NWS fails, a cached
forecast up to 12 h old is served with `stale: true`. The text forecast
often fails on its own; then `periods` is null and the hourly values are
still served. NWS asks for a User-Agent with contact details
(WEATHER_USER_AGENT). docs/runbook.md §9.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import get_settings
from app.http_retry import call_with_resilience

logger = logging.getLogger("whumpf.nws")

API = "https://api.weather.gov"
ATTRIBUTION = "NOAA/National Weather Service"
ATTRIBUTION_URL = "https://www.weather.gov/"
POINT_TTL_S = 7 * 24 * 3600.0
FORECAST_TTL = timedelta(minutes=45)
STALE_MAX = timedelta(hours=12)
HOURS = 48
PERIODS = 4
_POINTS_MAX = 5000
_FORECASTS_MAX = 2000

_HTTP = httpx.AsyncClient(timeout=10.0, follow_redirects=True)


class NotCovered(Exception):
    """NWS has no forecast for this point (outside the US, open ocean)."""


class Unavailable(Exception):
    """NWS can't be reached (or answered nonsense) and nothing usable is cached."""


@dataclass(frozen=True)
class Grid:
    office: str
    x: int
    y: int


# ── HTTP ──────────────────────────────────────────────────────────────────────

async def _get(path: str) -> httpx.Response:
    headers = {"User-Agent": get_settings().weather_user_agent, "Accept": "application/geo+json"}

    async def _do() -> httpx.Response:
        r = await _HTTP.get(f"{API}{path}", headers=headers)
        if r.status_code != 404:
            r.raise_for_status()
        return r

    return await call_with_resilience("nws", _do)


# ── /points: tap -> grid cell ─────────────────────────────────────────────────

_points: OrderedDict[tuple[float, float], tuple[Grid | None, float]] = OrderedDict()


async def grid_for(lat: float, lon: float) -> Grid:
    """The NWS grid cell for a point. Raises NotCovered, or httpx /
    CircuitOpenError if NWS can't be reached."""
    key = (round(lat, 3), round(lon, 3))
    cached = _points.get(key)
    if cached and time.monotonic() - cached[1] < POINT_TTL_S:
        _points.move_to_end(key)
        if cached[0] is None:
            raise NotCovered
        return cached[0]

    r = await _get(f"/points/{key[0]:.4f},{key[1]:.4f}")
    grid = None
    if r.status_code != 404:
        props = r.json().get("properties") or {}
        if props.get("gridId") and props.get("forecastGridData"):
            grid = Grid(str(props["gridId"]), int(props["gridX"]), int(props["gridY"]))
    _points[key] = (grid, time.monotonic())
    _points.move_to_end(key)
    while len(_points) > _POINTS_MAX:
        _points.popitem(last=False)
    if grid is None:
        raise NotCovered
    return grid


# ── /gridpoints: parse ────────────────────────────────────────────────────────

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$")


def parse_interval(valid_time: str) -> tuple[datetime, datetime]:
    """'2026-10-15T12:00:00+00:00/PT3H' -> (start, end) in UTC."""
    start_s, _, dur = valid_time.partition("/")
    m = _DURATION.match(dur)
    if not m or not any(m.groups()):
        raise ValueError(f"bad duration in {valid_time!r}")
    days, hours, minutes = (int(g or 0) for g in m.groups())
    start = datetime.fromisoformat(start_s).astimezone(UTC)
    return start, start + timedelta(days=days, hours=hours, minutes=minutes)


# NWS unit codes -> factor to our units (°F handled separately).
_TO_MPH = {"wmoUnit:km_h-1": 1 / 1.609344, "wmoUnit:m_s-1": 2.2369363}
_TO_IN = {"wmoUnit:mm": 1 / 25.4, "wmoUnit:m": 39.370079}
_SERIES = {
    "temperature": "temp_f",
    "windSpeed": "wind_mph",
    "windGust": "gust_mph",
    "windDirection": "wind_dir_deg",
    "probabilityOfPrecipitation": "pop_pct",
    "snowfallAmount": "snow_in",
}

Series = list[tuple[datetime, datetime, float | None]]


def _convert(field: str, uom: str, value: float | None) -> float | None:
    if value is None:
        return None
    if field == "temp_f":
        if uom == "wmoUnit:degC":
            return value * 9 / 5 + 32
        if uom == "wmoUnit:degF":
            return value
    elif field in ("wind_mph", "gust_mph"):
        if uom in _TO_MPH:
            return value * _TO_MPH[uom]
    elif field == "wind_dir_deg":
        if uom == "wmoUnit:degree_(angle)":
            return value
    elif field == "pop_pct":
        if uom == "wmoUnit:percent":
            return value
    elif field == "snow_in" and uom in _TO_IN:
        return value * _TO_IN[uom]
    raise ValueError(f"unexpected unit {uom!r} for {field}")


def parse_grid(raw: dict) -> dict[str, Any]:
    """The parts of a /gridpoints response we use, in our units. Raises
    ValueError on anything unexpected."""
    props = raw.get("properties") or {}
    series: dict[str, Series] = {}
    for nws_name, field in _SERIES.items():
        prop = props.get(nws_name) or {}
        uom = prop.get("uom", "")
        out: Series = []
        for v in prop.get("values") or []:
            start, end = parse_interval(v["validTime"])
            out.append((start, end, _convert(field, uom, v.get("value"))))
        series[field] = out
    if not series["temp_f"]:
        raise ValueError("no temperature values")
    elevation = (props.get("elevation") or {})
    elevation_m = elevation.get("value") if elevation.get("unitCode") == "wmoUnit:m" else None
    return {"updated": props.get("updateTime"), "elevation_m": elevation_m, "series": series}


def parse_periods(raw: dict) -> list[dict[str, Any]]:
    out = []
    for p in (raw.get("properties") or {}).get("periods") or []:
        out.append({
            "name": p.get("name"),
            "start": datetime.fromisoformat(p["startTime"]).astimezone(UTC),
            "end": datetime.fromisoformat(p["endTime"]).astimezone(UTC),
            "short": p.get("shortForecast"),
            "detailed": p.get("detailedForecast"),
        })
    return out


# ── forecast cache ────────────────────────────────────────────────────────────

# grid -> (parsed forecast, fetched at)
_forecasts: OrderedDict[Grid, tuple[dict[str, Any], datetime]] = OrderedDict()
_locks: dict[Grid, asyncio.Lock] = {}


async def _fetch(grid: Grid) -> dict[str, Any]:
    path = f"/gridpoints/{grid.office}/{grid.x},{grid.y}"
    raw, text = await asyncio.gather(_get(path), _get(f"{path}/forecast"), return_exceptions=True)
    if isinstance(raw, BaseException):
        raise raw
    if raw.status_code == 404:
        raise NotCovered
    data = parse_grid(raw.json())
    data["periods"] = None
    if isinstance(text, BaseException) or text.status_code == 404:
        logger.warning("NWS text forecast for %s failed: %r", path, text)
    else:
        try:
            data["periods"] = parse_periods(text.json())
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("NWS text forecast for %s unreadable: %r", path, exc)
    return data


async def forecast_for(grid: Grid, now: datetime) -> tuple[dict[str, Any], datetime, bool]:
    """(parsed forecast, fetched at, stale) for a grid cell. Raises
    NotCovered, or Unavailable when NWS fails and nothing usable is cached."""
    cached = _forecasts.get(grid)
    if cached and now - cached[1] < FORECAST_TTL:
        return cached[0], cached[1], False

    lock = _locks.setdefault(grid, asyncio.Lock())
    async with lock:
        cached = _forecasts.get(grid)  # another request may have refreshed it
        if cached and now - cached[1] < FORECAST_TTL:
            return cached[0], cached[1], False
        try:
            data = await _fetch(grid)
        except NotCovered:
            raise
        except Exception as exc:
            logger.warning("NWS forecast for %s failed: %r", grid, exc)
            if cached and now - cached[1] < STALE_MAX:
                return cached[0], cached[1], True
            raise Unavailable from exc
        _forecasts[grid] = (data, now)
        _forecasts.move_to_end(grid)
        while len(_forecasts) > _FORECASTS_MAX:
            old, _ = _forecasts.popitem(last=False)
            _locks.pop(old, None)
        return data, now, False


# ── response ──────────────────────────────────────────────────────────────────

def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _at(series: Series, t: datetime) -> float | None:
    for start, end, value in series:
        if start <= t < end:
            return value
    return None


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


def _ft(metres: float | None) -> int | None:
    return None if metres is None else round(metres / 0.3048)


def build(
    data: dict[str, Any], grid: Grid, now: datetime,
) -> dict[str, Any]:
    """The next HOURS hours from the current hour, the snow blocks that
    overlap them, and the next PERIODS text periods."""
    first = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    last = first + timedelta(hours=HOURS)
    s = data["series"]
    hours = []
    for i in range(HOURS):
        t = first + timedelta(hours=i)
        hours.append({
            "time": iso(t),
            "temp_f": _round(_at(s["temp_f"], t)),
            "wind_mph": _round(_at(s["wind_mph"], t)),
            "gust_mph": _round(_at(s["gust_mph"], t)),
            "wind_dir_deg": _round(_at(s["wind_dir_deg"], t)),
            "pop_pct": _round(_at(s["pop_pct"], t)),
        })
    snow = [
        {"start": iso(start), "end": iso(end), "snow_in": None if v is None else round(v, 1)}
        for start, end, v in s["snow_in"]
        if end > first and start < last
    ]
    periods = None
    if data["periods"] is not None:
        periods = [
            {"name": p["name"], "start": iso(p["start"]), "end": iso(p["end"]),
             "short": p["short"], "detailed": p["detailed"]}
            for p in data["periods"] if p["end"] > now
        ][:PERIODS]
    return {
        "grid": {"office": grid.office, "x": grid.x, "y": grid.y, "elevation_ft": _ft(data["elevation_m"])},
        "updated": data["updated"],
        "units": {"temp": "F", "wind": "mph", "snow": "in", "pop": "%"},
        "hours": hours,
        "snow": snow,
        "periods": periods,
        "attribution": ATTRIBUTION,
        "attribution_url": ATTRIBUTION_URL,
    }
