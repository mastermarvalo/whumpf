"""Spot forecast: GET /weather/forecast?lat=&lon=&t=

Deliberately open to signed-out installs: it needs a tile token (every
install has one, see app/tile_tokens.py) instead of sign-in, plus the
per-IP `weather` rate limit. Requiring the token keeps it from being a free
anonymous proxy to api.weather.gov under our User-Agent; abuse there gets
us blocked, not the abuser. The data is public NWS data. CLAUDE.md lists
this exception. docs/runbook.md §9.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from app import tile_tokens
from app.config import get_settings
from app.http_retry import CircuitOpenError
from app.rate_limit import client_ip
from app.regions import REGIONS
from app.services import cog_sampler, nws

logger = logging.getLogger("whumpf.weather")

_NO_STORE = {"Cache-Control": "no-store"}
_ELEVATION_TIMEOUT_S = 5.0


def require_weather_access(
    request: Request,
    t: str | None = Query(None, description="Tile token from POST /tiles/token"),
) -> None:
    """A valid tile token (any tier; tokenless is never allowed here), then
    the per-IP `weather` limit."""
    if t is None or tile_tokens.verify(t) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tile token required" if t is None else "Invalid or expired tile token",
            headers=_NO_STORE,
        )
    tile_tokens.enforce("weather", client_ip(request))


router = APIRouter(
    prefix="/weather", tags=["weather"], dependencies=[Depends(require_weather_access)]
)


def _region_at(lon: float, lat: float) -> str | None:
    for name, region in REGIONS.items():
        west, south, east, north = region.max_bounds
        if west <= lon <= east and south <= lat <= north:
            return name
    return None


async def _point_elevation_ft(lon: float, lat: float) -> int | None:
    """The tapped point's elevation from our DEM, or None outside our
    regions or if the DEM can't be read in time. Never fails the request."""
    region = _region_at(lon, lat)
    if region is None:
        return None
    try:
        metres = await asyncio.wait_for(
            asyncio.to_thread(cog_sampler.sample_elevation, lon, lat, region, get_settings()),
            _ELEVATION_TIMEOUT_S,
        )
    except Exception:
        logger.warning("DEM elevation for %.4f,%.4f failed", lat, lon, exc_info=True)
        return None
    return None if metres is None else round(metres / 0.3048)


@router.get("/forecast")
async def spot_forecast(
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
) -> JSONResponse:
    """The next 48 h at a point from NWS: hourly temperature, wind, gusts and
    precipitation chance; snow in NWS's own (usually 6 h) blocks; and the
    next four text periods. `grid.elevation_ft` is the elevation NWS
    forecast for (one value per 2.5 km cell); `point_elevation_ft` is the
    tapped point's, from our DEM. `stale: true` means NWS is down and this
    is the last forecast fetched (at most 12 h old)."""
    now = datetime.now(UTC)
    try:
        grid = await nws.grid_for(lat, lon)
        data, fetched, stale = await nws.forecast_for(grid, now)
    except nws.NotCovered:
        raise HTTPException(404, "no NWS forecast for this point", headers=_NO_STORE) from None
    except (nws.Unavailable, httpx.HTTPError, CircuitOpenError, KeyError, TypeError, ValueError) as exc:
        logger.warning("spot forecast for %.4f,%.4f failed: %r", lat, lon, exc)
        raise HTTPException(502, "NWS forecast unavailable", headers=_NO_STORE) from exc

    body = {
        "lat": lat,
        "lon": lon,
        **nws.build(data, grid, now),
        "point_elevation_ft": await _point_elevation_ft(lon, lat),
        "fetched": nws.iso(fetched),
        "stale": stale,
    }
    # Per point and per token, so only the app may reuse it, briefly.
    return JSONResponse(body, headers={"Cache-Control": "private, max-age=300"})
