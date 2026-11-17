"""NAIP aerial imagery at zooms 17-18: GET /tiles/naip/meta and
/tiles/naip/{z}/{x}/{y} (docs/runbook.md §18).

Below zoom 17 the app keeps using USGS's own imagery tiles. Every tile is
made once (app/services/naip.py) and kept in the offline pack cache, which
the pack worker reads for packs too. Under /tiles, so the tile token, its
rate limits and the Cloudflare cache rule apply; tiles a cache miss would
make also count against the global `naip_upstream` limit (503 when it's used
up), so no number of callers can push Microsoft past it.

Tile URLs from `meta` carry the index revision (?v=), so they can be cached
for a week and change when a new NAIP year comes in.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from app.config import validate_region
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.services import naip
from app.services.pack_cache import PackCache, PackTileError, UpstreamBusy
from app.tile_tokens import require_tile_access

router = APIRouter(prefix="/tiles", tags=["tiles"], dependencies=[Depends(require_tile_access)])
logger = logging.getLogger("whumpf.naip")

_CURRENT = {"Cache-Control": "public, max-age=604800"}
_UNVERSIONED = {"Cache-Control": "public, max-age=86400"}
_NO_STORE = {"Cache-Control": "no-store"}

_cache: PackCache | None = None


def _pack_cache() -> PackCache:
    """One per worker process, made on first use."""
    global _cache
    if _cache is None:
        _cache = PackCache(wait_when_busy=False)
    return _cache


async def _entry(region: str) -> dict | None:
    try:
        return ((await naip.read_pointer(region)) or {}).get("current")
    except (CircuitOpenError, httpx.HTTPError, ValueError) as exc:
        logger.warning("naip latest.json unreadable for %s: %r", region, exc)
        raise HTTPException(503, "NAIP imagery temporarily unavailable", headers=_NO_STORE) from exc


def _in_region(region: str, z: int, x: int, y: int) -> bool:
    west, south, east, north = naip.tile_lonlat_bounds(z, x, y)
    rw, rs, re_, rn = REGIONS[region].max_bounds
    return west < re_ and east > rw and south < rn and north > rs


@router.get("/naip/meta")
async def naip_meta(region: str = Query(...)) -> JSONResponse:
    validate_region(region)
    entry = await _entry(region)
    body: dict[str, Any] = {
        "region": region,
        "attribution": naip.ATTRIBUTION,
        "attribution_url": naip.ATTRIBUTION_URL,
        "bounds": REGIONS[region].max_bounds,
        "minzoom": naip.MINZOOM,
        "maxzoom": naip.MAXZOOM,
        "tile_size": naip.TILE_PX,
        "available": entry is not None,
    }
    if entry:
        body.update({
            "revision": entry["revision"],
            "years": entry.get("years", {}),
            "dates": entry.get("dates"),
            "valid": entry["valid"],
            "stale": datetime.now(UTC) - datetime.fromisoformat(entry["valid"]) > naip.STALE_AFTER,
            "tiles": f"/tiles/naip/{{z}}/{{x}}/{{y}}?region={region}&v={entry['revision']}",
        })
    return JSONResponse(body, headers={"Cache-Control": f"public, max-age={300 if entry else 60}"})


@router.get("/naip/{z}/{x}/{y}")
async def naip_tile(
    z: int, x: int, y: int,
    region: str = Query(...),
    v: str | None = Query(None, description="index revision from meta"),
) -> Response:
    validate_region(region)
    if not (naip.MINZOOM <= z <= naip.MAXZOOM) or not (0 <= x < 1 << z and 0 <= y < 1 << z):
        raise HTTPException(404, f"NAIP tiles are zooms {naip.MINZOOM}-{naip.MAXZOOM}")
    entry = await _entry(region)
    if not entry:
        raise HTTPException(503, "no NAIP imagery yet", headers=_NO_STORE)
    headers = _CURRENT if v == entry["revision"] else _UNVERSIONED
    if not _in_region(region, z, x, y):  # never cached: nothing to make
        return Response(status_code=204, headers=headers)
    try:
        data = await _pack_cache().get_tile("naip", region, z, x, y)
    except UpstreamBusy as exc:
        raise HTTPException(503, "NAIP imagery is busy; try again shortly",
                            headers={**_NO_STORE, "Retry-After": str(exc.retry_after)}) from exc
    except PackTileError as exc:
        logger.warning("naip tile %s/%d/%d/%d failed: %s", region, z, x, y, exc)
        raise HTTPException(503, "NAIP imagery temporarily unavailable",
                            headers={**_NO_STORE, "Retry-After": "30"}) from exc
    if not data:
        return Response(status_code=204, headers=headers)
    return Response(data, media_type="image/jpeg", headers=headers)
