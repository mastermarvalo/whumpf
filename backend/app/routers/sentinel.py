"""Recent Sentinel-2 imagery: GET /tiles/sentinel/meta, /tiles/sentinel/dates
and /tiles/sentinel/{true|false}/{z}/{x}/{y} (docs/runbook.md §19).

The daily job (app/jobs/sentinel_refresh.py) writes the COGs and latest.json;
TiTiler renders the tiles (PNG, transparent where there is no clear view in
the window). Under /tiles, so the tile token, its limits and the Cloudflare
cache rule apply. Tiles and the dates grid go through app/entitlements.py
("sentinel"), today open to every tier; meta always answers and says
`access`, so the app can show the layer as locked if it ever isn't.

Tile and dates URLs from `meta` carry the revision (?v=): cached for a day,
and a new revision every night gives new URLs.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from app import entitlements
from app.config import validate_region
from app.http_retry import CircuitOpenError
from app.regions import REGIONS
from app.routers.tiles import cog_tile
from app.services import sentinel
from app.tile_tokens import require_tile_access

router = APIRouter(prefix="/tiles", tags=["tiles"], dependencies=[Depends(require_tile_access)])
logger = logging.getLogger("whumpf.sentinel")

LAYER = "sentinel"  # the name in entitlements.ALLOWED_TIERS and /packs
MINZOOM = 6

_CURRENT = {"Cache-Control": "public, max-age=86400"}
_UNVERSIONED = {"Cache-Control": "public, max-age=900"}
_NO_STORE = {"Cache-Control": "no-store"}

# ~100-150 KB a PNG tile.
_CACHE: OrderedDict[tuple, bytes] = OrderedDict()
_CACHE_MAX = 512


async def _entry(region: str) -> dict | None:
    try:
        return ((await sentinel.read_pointer(region)) or {}).get("current")
    except (CircuitOpenError, httpx.HTTPError, ValueError) as exc:
        logger.warning("sentinel latest.json unreadable for %s: %r", region, exc)
        raise HTTPException(503, "Sentinel-2 imagery temporarily unavailable", headers=_NO_STORE) from exc


def _is_stale(entry: dict) -> bool:
    return datetime.now(UTC) - datetime.fromisoformat(entry["valid"]) > sentinel.STALE_AFTER


@router.get("/sentinel/meta")
async def sentinel_meta(request: Request, region: str = Query(...)) -> JSONResponse:
    validate_region(region)
    entry = await _entry(region)
    dates = entry.get("dates") if entry else None
    body: dict[str, Any] = {
        "region": region,
        "attribution": sentinel.attribution(dates),
        "attribution_url": sentinel.ATTRIBUTION_URL,
        "bounds": REGIONS[region].max_bounds,
        "minzoom": MINZOOM,
        "maxzoom": sentinel.MAXZOOM,
        "tile_size": 256,
        "products": list(sentinel.PRODUCTS),
        "window_days": sentinel.WINDOW_DAYS,
        "access": entitlements.access(entitlements.tile_tier(request), LAYER),
        "available": entry is not None,
    }
    if entry:
        rev = entry["revision"]
        body.update({
            "revision": rev,
            "dates": dates,
            "newest_scene": entry.get("newest_scene"),
            "coverage": entry.get("coverage"),
            "valid": entry["valid"],
            "stale": _is_stale(entry),
            "tiles": {p: f"/tiles/sentinel/{p}/{{z}}/{{x}}/{{y}}?region={region}&v={rev}"
                      for p in sentinel.PRODUCTS},
            "dates_url": f"/tiles/sentinel/dates?region={region}&v={rev}",
        })
    return JSONResponse(body, headers={"Cache-Control": f"public, max-age={300 if entry else 60}"})


@router.get("/sentinel/dates")
async def sentinel_dates(request: Request, region: str = Query(...), v: str | None = Query(None)) -> Response:
    """The capture day per ~1 km cell (sentinel.dates_doc), gzipped JSON."""
    validate_region(region)
    entitlements.require(entitlements.tile_tier(request), LAYER)
    entry = await _entry(region)
    if not entry:
        raise HTTPException(503, "no Sentinel-2 imagery yet", headers=_NO_STORE)
    try:
        body = await sentinel.read_dates(entry["keys"]["dates"])
    except (CircuitOpenError, httpx.HTTPError) as exc:
        logger.warning("sentinel dates unreadable for %s: %r", region, exc)
        raise HTTPException(503, "Sentinel-2 imagery temporarily unavailable", headers=_NO_STORE) from exc
    if body is None:
        raise HTTPException(503, "Sentinel-2 imagery temporarily unavailable", headers=_NO_STORE)
    headers = _CURRENT if v == entry["revision"] else _UNVERSIONED
    return Response(body, media_type="application/json", headers={**headers, "Content-Encoding": "gzip"})


@router.get("/sentinel/{product}/{z}/{x}/{y}")
async def sentinel_tile(
    request: Request, product: str, z: int, x: int, y: int,
    region: str = Query(...), v: str | None = Query(None, description="revision from meta"),
) -> Response:
    validate_region(region)
    if product not in sentinel.PRODUCTS:
        raise HTTPException(404, "no such product")
    if not (MINZOOM <= z <= sentinel.MAXZOOM) or not (0 <= x < 1 << z and 0 <= y < 1 << z):
        raise HTTPException(404, f"Sentinel-2 tiles are zooms {MINZOOM}-{sentinel.MAXZOOM}")
    entitlements.require(entitlements.tile_tier(request), LAYER)
    entry = await _entry(region)
    if not entry:
        raise HTTPException(503, "no Sentinel-2 imagery yet", headers=_NO_STORE)
    headers = _CURRENT if v == entry["revision"] else _UNVERSIONED
    return await cog_tile(
        _CACHE, _CACHE_MAX, (region, entry["revision"], product, z, x, y), entry["keys"][product],
        {}, headers, f"sentinel {product}", z, x, y, fmt="png",
    )
