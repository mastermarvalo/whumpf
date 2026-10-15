"""CAIC avalanche forecast and observations — stale-while-revalidate caching.

Deliberately public (no sign-in): signed-out users and the old site use it and
the data is public CAIC data. Rate-limited per client IP instead.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.http_retry import call_with_resilience
from app.rate_limit import client_ip
from app.tile_tokens import enforce


def _limit_by_ip(request: Request) -> None:
    enforce("avalanche", client_ip(request))


router = APIRouter(prefix="/avalanche", tags=["avalanche"], dependencies=[Depends(_limit_by_ip)])
logger = logging.getLogger("whumpf.avalanche")

_FORECAST_TTL = timedelta(hours=1)
_OBS_TTL      = timedelta(minutes=30)
_AVID_TTL     = timedelta(hours=1)
_ZONES_TTL    = timedelta(hours=24)

_MAPLAYER_URL = "https://api.avalanche.org/v2/public/products/map-layer"
_AVID_URL     = "https://avalanche.state.co.us/api-proxy/avid"
_OBS_URL      = "https://api.avalanche.state.co.us/api/v2/observation_reports"
_ZONES_URL    = "https://api.avalanche.state.co.us/api/v2/zones"

_HTTP = httpx.AsyncClient(
    timeout=15.0,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

# ── per-endpoint cache + lock ─────────────────────────────────────────────────

_forecast_cache: tuple[dict, datetime] | None = None
_forecast_lock  = asyncio.Lock()

_obs_cache: tuple[dict, datetime] | None = None
_obs_lock  = asyncio.Lock()

_avid_cache: tuple[tuple[list, dict], datetime] | None = None
_avid_lock  = asyncio.Lock()

_zones_cache: tuple[list[tuple[str, dict]], datetime] | None = None
_zones_lock  = asyncio.Lock()


# ── map-layer forecast (polygon fill layer) ────────────────────────────────────

async def _do_fetch_forecast() -> dict:
    global _forecast_cache

    async def _do() -> httpx.Response:
        r = await _HTTP.get(_MAPLAYER_URL, params={"type": "forecast", "center_id": "CAIC"})
        r.raise_for_status()
        return r

    try:
        resp = await call_with_resilience("caic-forecast", _do)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"avalanche.org error: {exc}") from exc
    raw = resp.json()
    features = [f for f in raw.get("features", []) if f.get("properties", {}).get("center_id") == "CAIC"]
    data = {**raw, "features": features}
    _forecast_cache = (data, datetime.utcnow())
    logger.info("CAIC forecast cached (%d zones)", len(features))
    return data


async def _bg_refresh_forecast() -> None:
    if _forecast_lock.locked():
        return
    async with _forecast_lock:
        if _forecast_cache is not None:
            _, fetched_at = _forecast_cache
            if datetime.utcnow() - fetched_at < _FORECAST_TTL:
                return
        try:
            await _do_fetch_forecast()
        except Exception as exc:
            logger.warning("CAIC forecast background refresh failed: %s", exc)


async def get_forecast() -> dict:
    """SWR: return cached data instantly; refresh in background when stale."""
    global _forecast_cache
    if _forecast_cache is not None:
        data, fetched_at = _forecast_cache
        if datetime.utcnow() - fetched_at < _FORECAST_TTL:
            return data
        asyncio.ensure_future(_bg_refresh_forecast())
        return data
    async with _forecast_lock:
        if _forecast_cache is not None:
            return _forecast_cache[0]
        return await _do_fetch_forecast()


@router.get("/forecast")
async def caic_forecast() -> dict:
    """CAIC danger-zone GeoJSON — cached 1 hour, stale-while-revalidate."""
    try:
        return await get_forecast()
    except Exception as exc:
        logger.error("CAIC forecast fetch failed: %s", exc)
        raise HTTPException(502, "avalanche.org unavailable") from exc


# ── AVID detail (danger rose + problems) ──────────────────────────────────────

async def _do_fetch_avid() -> tuple[list, dict]:
    global _avid_cache

    async def _do() -> tuple[httpx.Response, httpx.Response]:
        products_resp, areas_resp = await asyncio.gather(
            _HTTP.get(_AVID_URL, params={"_api_proxy_uri": "/products/all?includeExpired=true"}),
            _HTTP.get(_AVID_URL, params={
                "_api_proxy_uri": "/products/all/area?productType=avalancheforecast&includeExpired=true"
            }),
        )
        products_resp.raise_for_status()
        areas_resp.raise_for_status()
        return products_resp, areas_resp

    try:
        products_resp, areas_resp = await call_with_resilience("caic-avid", _do)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"AVID error: {exc}") from exc
    products = [p for p in products_resp.json() if p.get("type") == "avalancheforecast"]
    areas = areas_resp.json()
    result = (products, areas)
    _avid_cache = (result, datetime.utcnow())
    return result


async def _fetch_avid() -> tuple[list, dict]:
    global _avid_cache
    if _avid_cache is not None:
        result, fetched_at = _avid_cache
        if datetime.utcnow() - fetched_at < _AVID_TTL:
            return result
    async with _avid_lock:
        if _avid_cache is not None:
            result, fetched_at = _avid_cache
            if datetime.utcnow() - fetched_at < _AVID_TTL:
                return result
        return await _do_fetch_avid()


# ── named backcountry zones (labels for AVID forecast areas) ───────────────────
# AVID forecast areas carry only a hash id. CAIC's named backcountry zones
# ("Vail & Summit County") give the tapped point a human label. Polygons change
# rarely, so they're cached 24 h; a failed refresh keeps the last good copy.

async def _do_fetch_zones() -> list[tuple[str, dict]]:
    global _zones_cache

    async def _do() -> list[tuple[str, dict]]:
        r = await _HTTP.get(_ZONES_URL)
        r.raise_for_status()
        leaves = [
            z for z in r.json()
            if z.get("category") == "backcountry" and z.get("is_leaf") and z.get("geojson_url")
        ]
        geo_resps = await asyncio.gather(*(_HTTP.get(z["geojson_url"]) for z in leaves))
        zones: list[tuple[str, dict]] = []
        for z, gr in zip(leaves, geo_resps):
            gr.raise_for_status()
            for feat in gr.json().get("features", []):
                geom = feat.get("geometry") or {}
                if geom.get("type") in ("Polygon", "MultiPolygon"):
                    name = (feat.get("properties") or {}).get("name") or z.get("title") or ""
                    zones.append((name, geom))
        return zones

    try:
        zones = await call_with_resilience("caic-zones", _do)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"CAIC zones error: {exc}") from exc
    if not zones:
        raise RuntimeError("CAIC zones response had no backcountry polygons")
    _zones_cache = (zones, datetime.utcnow())
    logger.info("CAIC zones cached (%d)", len(zones))
    return zones


async def _bg_refresh_zones() -> None:
    if _zones_lock.locked():
        return
    async with _zones_lock:
        if _zones_cache is not None:
            _, fetched_at = _zones_cache
            if datetime.utcnow() - fetched_at < _ZONES_TTL:
                return
        try:
            await _do_fetch_zones()
        except Exception as exc:
            logger.warning("CAIC zones background refresh failed: %s", exc)


async def get_zones() -> list[tuple[str, dict]]:
    """SWR: return cached data instantly; refresh in background when stale."""
    if _zones_cache is not None:
        zones, fetched_at = _zones_cache
        if datetime.utcnow() - fetched_at >= _ZONES_TTL:
            asyncio.ensure_future(_bg_refresh_zones())
        return zones
    async with _zones_lock:
        if _zones_cache is not None:
            return _zones_cache[0]
        return await _do_fetch_zones()


async def _zone_name_for_point(lat: float, lng: float) -> str | None:
    """Name of the CAIC backcountry zone containing (lat, lng), or None if the
    point is outside all zones or the zones can't be loaded."""
    try:
        zones = await get_zones()
    except Exception as exc:
        logger.warning("CAIC zones unavailable, labelling by area id: %s", exc)
        return None
    for name, geom in zones:
        if name and _in_multipolygon(lat, lng, geom):
            return name
    return None


# ── point-in-polygon (ray casting) ────────────────────────────────────────────

def _ray_cross(lat: float, lng: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _in_multipolygon(lat: float, lng: float, geom: dict) -> bool:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        exterior = poly[0]
        if not _ray_cross(lat, lng, exterior):
            continue
        if any(_ray_cross(lat, lng, hole) for hole in poly[1:]):
            continue
        return True
    return False


def _centroid_dist(lat: float, lng: float, centroid: list) -> float:
    return math.hypot(lng - centroid[0], lat - centroid[1])


# ── response helpers ───────────────────────────────────────────────────────────

_PROBLEM_LABELS: dict[str, str] = {
    "wetLoose": "Wet Loose",
    "windSlab": "Wind Slab",
    "stormSlab": "Storm Slab",
    "persistentSlab": "Persistent Slab",
    "deepPersistentSlab": "Deep Persistent Slab",
    "cornice": "Cornice",
    "glide": "Glide Avalanche",
}

_LIKELIHOOD_LABELS: dict[str, str] = {
    "unlikely": "Unlikely",
    "possible": "Possible",
    "likely": "Likely",
    "veryLikely": "Very Likely",
    "certain": "Certain",
}


def _parse_problems(raw: list) -> list:
    out = []
    for p in raw:
        aspects: set[str] = set()
        elevations: set[str] = set()
        for ae in p.get("aspectElevations", []):
            parts = ae.rsplit("_", 1)
            if len(parts) == 2:
                aspects.add(parts[0].upper())
                elevations.add(parts[1])
        size = p.get("expectedSize", {})
        out.append({
            "type": p.get("type", ""),
            "label": _PROBLEM_LABELS.get(p.get("type", ""), p.get("type", "")),
            "likelihood": _LIKELIHOOD_LABELS.get(p.get("likelihood", ""), p.get("likelihood", "")),
            "size_min": size.get("min", ""),
            "size_max": size.get("max", ""),
            "aspects": sorted(aspects, key=lambda a: ["N","NE","E","SE","S","SW","W","NW"].index(a) if a in ["N","NE","E","SE","S","SW","W","NW"] else 99),
            "elevations": sorted(elevations, key=lambda e: {"alp": 0, "tln": 1, "btl": 2}.get(e, 9)),
            "aspect_elevations": p.get("aspectElevations", []),
        })
    return out


def _build_zone_detail(product: dict) -> dict:
    day0 = product.get("dangerRatings", {}).get("days", [{}])[0]
    problems_raw = product.get("avalancheProblems", {}).get("days", [[]])[0]
    issue_dt = product.get("issueDateTime", "")
    valid_date = issue_dt[:10] if issue_dt else ""
    return {
        "forecaster": product.get("forecaster", ""),
        "valid_date": valid_date,
        "danger": {
            "alp": day0.get("alp", "noForecast"),
            "tln": day0.get("tln", "noForecast"),
            "btl": day0.get("btl", "noForecast"),
        },
        "problems": _parse_problems(problems_raw),
        "link": "https://avalanche.state.co.us/forecasts/backcountry",
    }


async def get_zone_detail_for_point(lat: float, lng: float) -> dict | None:
    """Resolve the CAIC zone forecast at (lat, lng) → a CaicZoneDetail dict with
    an added ``zone`` label (CAIC backcountry zone name containing the point,
    else the areaId) and ``zone_id`` (the areaId), or None if no zone matches.

    Point-in-polygon first, falling back to nearest centroid. Reused by the
    /zone_detail endpoint and by trip creation (to freeze a forecast snapshot).
    """
    products, areas = await _fetch_avid()

    area_map: dict[str, dict] = {}
    for feat in areas.get("features", []):
        aid = feat["properties"].get("id")
        if aid:
            area_map[aid] = {
                "geometry": feat["geometry"],
                "centroid": feat["properties"].get("centroid", [0, 0]),
                "name": feat["properties"].get("name") or aid,
            }

    zone_name = await _zone_name_for_point(lat, lng)

    def _detail(product: dict) -> dict:
        aid = product.get("areaId")
        out = _build_zone_detail(product)
        out["zone"] = zone_name or (area_map.get(aid, {}).get("name") if aid else None) or aid or ""
        out["zone_id"] = aid or ""
        return out

    for product in products:
        aid = product.get("areaId")
        if aid and aid in area_map and _in_multipolygon(lat, lng, area_map[aid]["geometry"]):
            return _detail(product)

    best_product, best_dist = None, float("inf")
    for product in products:
        aid = product.get("areaId")
        if aid and aid in area_map:
            d = _centroid_dist(lat, lng, area_map[aid]["centroid"])
            if d < best_dist:
                best_dist, best_product = d, product

    return _detail(best_product) if best_product is not None else None


@router.get("/zone_detail")
async def caic_zone_detail(
    lat: float = Query(..., ge=-90, le=90, description="Clicked latitude"),
    lng: float = Query(..., ge=-180, le=180, description="Clicked longitude"),
) -> dict:
    """Full danger rose + avalanche problems for the CAIC zone at (lat, lng)."""
    try:
        detail = await get_zone_detail_for_point(lat, lng)
    except Exception as exc:
        logger.error("AVID fetch failed: %s", exc)
        raise HTTPException(502, "AVID unavailable") from exc
    if detail is None:
        raise HTTPException(404, "No CAIC zone found")
    return detail


# ── field observations ─────────────────────────────────────────────────────────

def _obs_type(r: dict) -> str:
    if r.get("caught_in_avalanche"):
        return "caught"
    if r.get("saw_avalanche") or r.get("triggered_avalanche"):
        return "avy"
    return "field"


def _build_obs_feature(r: dict) -> dict | None:
    lat = r.get("latitude")
    lon = r.get("longitude")
    if not lat or not lon:
        return None
    zone = r.get("backcountry_zone") or {}
    zone_title = zone.get("title", "") if isinstance(zone, dict) else ""
    links = r.get("related_report_links") or {}
    link = links.get("external_canonical_report", "")
    assets = r.get("assets") or []
    thumb = next((a["thumb_url"] for a in assets if a.get("thumb_url")), None)
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {
            "id": r.get("report_id") or r.get("id", ""),
            "obs_type": _obs_type(r),
            "observer": r.get("full_name") or r.get("firstname", ""),
            "organization": r.get("organization", ""),
            "is_anonymous": r.get("is_anonymous", False),
            "observed_at": r.get("observed_at", ""),
            "zone": zone_title,
            "route": (r.get("route") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "avy_count": r.get("avalanche_observations_count", 0),
            "saw_avy": bool(r.get("saw_avalanche")),
            "triggered_avy": bool(r.get("triggered_avalanche")),
            "caught": bool(r.get("caught_in_avalanche")),
            "link": link,
            "thumb": thumb,
        },
    }


async def _do_fetch_obs() -> dict:
    global _obs_cache
    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")

    async def _do() -> httpx.Response:
        r = await _HTTP.get(_OBS_URL, params={
            "r[observed_at_gteq]": since,
            "r[sorts][]": "observed_at desc",
            "per": "500",
        })
        r.raise_for_status()
        return r

    try:
        resp = await call_with_resilience("caic-obs", _do)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"CAIC observations error: {exc}") from exc
    records = resp.json()
    features = [f for r in records if (f := _build_obs_feature(r)) is not None]
    data = {
        "type": "FeatureCollection",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "features": features,
    }
    _obs_cache = (data, datetime.utcnow())
    logger.info("CAIC observations cached (%d features)", len(features))
    return data


async def _bg_refresh_obs() -> None:
    if _obs_lock.locked():
        return
    async with _obs_lock:
        if _obs_cache is not None:
            _, fetched_at = _obs_cache
            if datetime.utcnow() - fetched_at < _OBS_TTL:
                return
        try:
            await _do_fetch_obs()
        except Exception as exc:
            logger.warning("CAIC observations background refresh failed: %s", exc)


async def get_observations() -> dict:
    """SWR: return cached data instantly; refresh in background when stale."""
    global _obs_cache
    if _obs_cache is not None:
        data, fetched_at = _obs_cache
        if datetime.utcnow() - fetched_at < _OBS_TTL:
            return data
        asyncio.ensure_future(_bg_refresh_obs())
        return data
    async with _obs_lock:
        if _obs_cache is not None:
            return _obs_cache[0]
        return await _do_fetch_obs()


@router.get("/observations")
async def caic_observations() -> dict:
    """CAIC field observations as GeoJSON — last 7 days, 30-min SWR cache."""
    try:
        return await get_observations()
    except Exception as exc:
        logger.error("CAIC observations fetch failed: %s", exc)
        raise HTTPException(502, "CAIC observations unavailable") from exc
