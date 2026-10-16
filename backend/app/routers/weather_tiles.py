"""Weather map layers: snow depth, current temperature, wind now and radar.

Same pattern as /tiles/snowfall (routers/tiles.py): cron jobs write COGs and
a latest.json per region into MinIO (app/services/rasterlayer.py), and these
routes serve `meta` plus TiTiler-rendered tiles. Everything is under /tiles,
so the tile token, its rate limits and the Cloudflare cache rule apply.

Tile URLs from `meta` carry the revision (?v=) or, for radar, the frame time
in the path, so they can be cached for a day and never need a purge. Wind
is not tiles but one GeoJSON file of arrows per region and hour, with its
revision in the path (app/services/wind.py).
docs/runbook.md §9.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Response
from fastapi.responses import JSONResponse

from app.config import validate_region
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.routers.tiles import _MAX_Z, cog_tile
from app.services import radar, snowdepth, temperature, wind
from app.tile_tokens import require_tile_access

router = APIRouter(
    prefix="/tiles", tags=["tiles"], dependencies=[Depends(require_tile_access)]
)
logger = logging.getLogger("whumpf.weather_tiles")

_CURRENT = {"Cache-Control": "public, max-age=86400"}
_UNVERSIONED = {"Cache-Control": "public, max-age=900"}
_NO_STORE = {"Cache-Control": "no-store"}


@dataclass(frozen=True)
class Layer:
    """A layer with one served revision per region (snow depth, temperature)."""

    name: str
    units: str
    attribution: str
    attribution_url: str
    maxzoom: int
    nodata: float
    colormap: str
    legend: Callable[[], list[dict[str, Any]]]
    read_pointer: Callable[[str], Awaitable[dict | None]]
    stale_after: timedelta
    cache: OrderedDict[tuple, bytes] = field(default_factory=OrderedDict)
    cache_max: int = 512


SNOWDEPTH = Layer(
    name="snowdepth",
    units="in",
    attribution=snowdepth.ATTRIBUTION,
    attribution_url=snowdepth.ATTRIBUTION_URL,
    maxzoom=snowdepth.MAXZOOM,
    nodata=snowdepth.NODATA,
    colormap=snowdepth.TITILER_COLORMAP,
    legend=snowdepth.legend,
    read_pointer=lambda region: snowdepth.read_pointer(region),
    stale_after=snowdepth.STALE_AFTER,
)

TEMPERATURE = Layer(
    name="temperature",
    units="F",
    attribution=temperature.ATTRIBUTION,
    attribution_url=temperature.ATTRIBUTION_URL,
    maxzoom=temperature.MAXZOOM,
    nodata=temperature.NODATA,
    colormap=temperature.TITILER_COLORMAP,
    legend=temperature.legend,
    read_pointer=lambda region: temperature.read_pointer(region),
    stale_after=temperature.STALE_AFTER,
)


def _check_tile(z: int, x: int, y: int) -> None:
    if z < 0 or z > _MAX_Z or x < 0 or y < 0 or x >= (1 << z) or y >= (1 << z):
        raise HTTPException(400, "tile coordinates out of range")


async def _pointer(name: str, read: Callable[[str], Awaitable[dict | None]], region: str) -> dict:
    try:
        return await read(region) or {}
    except (CircuitOpenError, httpx.HTTPError, ValueError) as exc:
        logger.warning("%s latest.json unreadable for %s: %r", name, region, exc)
        raise HTTPException(503, f"{name} data temporarily unavailable", headers=_NO_STORE) from exc


def _is_stale(valid: str, stale_after: timedelta) -> bool:
    return datetime.now(UTC) - datetime.fromisoformat(valid) > stale_after


async def _layer_meta(layer: Layer, region: str) -> JSONResponse:
    validate_region(region)
    entry = (await _pointer(layer.name, layer.read_pointer, region)).get("current")
    body: dict[str, Any] = {
        "region": region,
        "units": layer.units,
        "attribution": layer.attribution,
        "attribution_url": layer.attribution_url,
        "bounds": REGIONS[region].max_bounds,
        "maxzoom": layer.maxzoom,
        "legend": layer.legend(),
        "available": entry is not None,
    }
    if entry:
        body.update({k: v for k, v in entry.items() if k not in ("key", "etag", "last_modified")})
        body["stale"] = _is_stale(entry["valid"], layer.stale_after)
        body["tiles"] = (
            f"/tiles/{layer.name}/{{z}}/{{x}}/{{y}}?region={region}&v={entry['revision']}"
        )
    return JSONResponse(body, headers={"Cache-Control": f"public, max-age={300 if entry else 60}"})


async def _layer_tile(layer: Layer, region: str, z: int, x: int, y: int, v: str | None) -> Response:
    validate_region(region)
    _check_tile(z, x, y)
    entry = (await _pointer(layer.name, layer.read_pointer, region)).get("current")
    if not entry:
        raise HTTPException(503, f"no {layer.name} data yet", headers=_NO_STORE)
    headers = _CURRENT if v == entry["revision"] else _UNVERSIONED
    return await cog_tile(
        layer.cache, layer.cache_max, (region, entry["revision"], z, x, y), entry["key"],
        {"nodata": str(int(layer.nodata)), "colormap": layer.colormap},
        headers, layer.name, z, x, y,
    )


# ── snow depth (NOHRSC SNODAS) ────────────────────────────────────────────────

@router.get("/snowdepth/meta")
async def snowdepth_meta(region: str = "colorado") -> JSONResponse:
    """Snow depth: valid time (06Z daily), versioned tile URL, legend in inches."""
    return await _layer_meta(SNOWDEPTH, region)


@router.get("/snowdepth/{z}/{x}/{y}")
async def snowdepth_tile(z: int, x: int, y: int, region: str = "colorado", v: str | None = None) -> Response:
    """Snow depth as a stepped colour PNG (inches, see snowdepth.SCALE)."""
    return await _layer_tile(SNOWDEPTH, region, z, x, y, v)


# ── current temperature (RTMA) ────────────────────────────────────────────────

@router.get("/temperature/meta")
async def temperature_meta(region: str = "colorado") -> JSONResponse:
    """2 m temperature analysis: valid hour, versioned tile URL, legend in °F."""
    return await _layer_meta(TEMPERATURE, region)


@router.get("/temperature/{z}/{x}/{y}")
async def temperature_tile(z: int, x: int, y: int, region: str = "colorado", v: str | None = None) -> Response:
    """2 m temperature as a stepped colour PNG (°F, see temperature.SCALE)."""
    return await _layer_tile(TEMPERATURE, region, z, x, y, v)


# ── wind now (RTMA) ───────────────────────────────────────────────────────────
# One gzipped GeoJSON file per region and hour instead of tiles: one request
# per hour shown, whatever the zoom (app/services/wind.py). `hours` is a list
# so forecast hours can join the analysis later without a new endpoint.

_WIND_GONE = {"Cache-Control": "public, max-age=60"}


@router.get("/wind/meta")
async def wind_meta(region: str = "colorado") -> JSONResponse:
    """10 m wind analysis: valid hour, the arrow file per hour, the zoom
    bands for thinning the arrows (`levels`), speeds in mph and directions
    in degrees true the wind blows from."""
    validate_region(region)
    entry = (await _pointer("wind", wind.read_pointer, region)).get("current")
    body: dict[str, Any] = {
        "region": region,
        "units": "mph",
        "direction": "from",
        "attribution": wind.ATTRIBUTION,
        "attribution_url": wind.ATTRIBUTION_URL,
        "bounds": REGIONS[region].max_bounds,
        "step_deg": wind.STEP_DEG,
        "levels": wind.LEVELS,
        "available": entry is not None,
        "hours": [],
    }
    if entry:
        body.update({k: entry[k] for k in ("valid", "revision", "fetched", "source", "max_mph", "max_gust_mph", "points")})
        body["stale"] = _is_stale(entry["valid"], wind.STALE_AFTER)
        body["hours"] = [{
            "time": entry["valid"],
            "kind": "analysis",
            "url": f"/tiles/wind/{entry['revision']}.geojson?region={region}",
        }]
    return JSONResponse(body, headers={"Cache-Control": f"public, max-age={300 if entry else 60}"})


@router.get("/wind/{revision}.geojson")
async def wind_arrows(revision: str, region: str = "colorado") -> Response:
    """The arrows for one hour as GeoJSON points with `dir` (degrees, from),
    `mph`, `gust_mph` and `lvl`. Never changes once written, so it is cached
    for a day. 404 once the file has been cleaned up (after 7 days)."""
    validate_region(region)
    if not wind.REVISION.match(revision):
        raise HTTPException(404, "no such wind hour", headers=_WIND_GONE)
    try:
        body = await wind.read_arrows(region, revision)
    except (CircuitOpenError, httpx.HTTPError) as exc:
        logger.warning("wind %s unreadable for %s: %r", revision, region, exc)
        raise HTTPException(503, "wind data temporarily unavailable", headers=_NO_STORE) from exc
    if body is None:
        raise HTTPException(404, "no such wind hour", headers=_WIND_GONE)
    return Response(
        content=body, media_type="application/geo+json",
        headers={**_CURRENT, "Content-Encoding": "gzip"},
    )


# ── radar (MRMS) ──────────────────────────────────────────────────────────────
# Frames sit on 10-minute marks and never change once written, so a frame's
# tiles are cached for a day; the frame time in the path is the version.

_RADAR_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_RADAR_CACHE_MAX = 1024  # ~13 frames × the tiles in view, mostly transparent
_RADAR_GONE = {"Cache-Control": "public, max-age=60"}


async def _radar_frames(region: str) -> list[dict]:
    return (await _pointer("radar", radar.read_pointer, region)).get("frames", [])


@router.get("/radar/meta")
async def radar_meta(region: str = "colorado") -> JSONResponse:
    """Radar frames of the last two hours, oldest first, each with its tile
    URL; `stale` once the newest is over 20 min old; legend in dBZ."""
    validate_region(region)
    now = datetime.now(UTC)
    frames = [
        f for f in await _radar_frames(region)
        if datetime.fromisoformat(f["time"]) >= now - radar.WINDOW
    ]
    body: dict[str, Any] = {
        "region": region,
        "units": "dBZ",
        "attribution": radar.ATTRIBUTION,
        "attribution_url": radar.ATTRIBUTION_URL,
        "bounds": REGIONS[region].max_bounds,
        "maxzoom": radar.MAXZOOM,
        "legend": radar.legend(),
        "interval_min": int(radar.FRAME.total_seconds() // 60),
        "available": bool(frames),
        "frames": [
            {
                "time": f["time"],
                "observed": f["observed"],
                "max_dbz": f.get("max_dbz"),
                "tiles": (
                    f"/tiles/radar/{radar.frame_id(datetime.fromisoformat(f['time']))}"
                    f"/{{z}}/{{x}}/{{y}}?region={region}"
                ),
            }
            for f in frames
        ],
    }
    if frames:
        body["latest"] = frames[-1]["time"]
        body["stale"] = _is_stale(frames[-1]["observed"], radar.STALE_AFTER)
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=60"})


@router.get("/radar/{frame}/{z}/{x}/{y}")
async def radar_tile(
    z: int, x: int, y: int,
    frame: str = Path(pattern=r"^\d{12}$", description="YYYYMMDDHHMM from /tiles/radar/meta"),
    region: str = "colorado",
) -> Response:
    """One radar frame as a stepped colour PNG (dBZ, see radar.SCALE). Under
    5 dBZ is transparent. 404 once the frame has left the two-hour window."""
    validate_region(region)
    _check_tile(z, x, y)
    entry = next(
        (f for f in await _radar_frames(region)
         if radar.frame_id(datetime.fromisoformat(f["time"])) == frame),
        None,
    )
    if entry is None:
        raise HTTPException(404, "no such radar frame", headers=_RADAR_GONE)
    return await cog_tile(
        _RADAR_CACHE, _RADAR_CACHE_MAX, (region, entry["key"], z, x, y), entry["key"],
        {"nodata": str(int(radar.NODATA)), "colormap": radar.TITILER_COLORMAP},
        _CURRENT, f"radar {frame}", z, x, y,
    )
