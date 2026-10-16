"""Weather map layers: snow depth, current temperature and radar.

Same pattern as /tiles/snowfall (routers/tiles.py): cron jobs write COGs and
a latest.json per region into MinIO (app/services/rasterlayer.py), and these
routes serve `meta` plus TiTiler-rendered tiles. Everything is under /tiles,
so the tile token, its rate limits and the Cloudflare cache rule apply.

Tile URLs from `meta` carry the revision (?v=) or, for radar, the frame time
in the path, so they can be cached for a day and never need a purge.
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
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from app.config import validate_region
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.routers.tiles import _MAX_Z, cog_tile
from app.services import snowdepth
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
