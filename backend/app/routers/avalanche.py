"""CAIC avalanche forecast proxy with 1-hour in-process cache."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/avalanche", tags=["avalanche"])
logger = logging.getLogger("whumpf.avalanche")

_CACHE_TTL = timedelta(hours=1)
_OBS_CACHE_TTL = timedelta(minutes=30)

_MAPLAYER_URL = "https://api.avalanche.org/v2/public/products/map-layer"
_AVID_URL = "https://avalanche.state.co.us/api-proxy/avid"
_OBS_URL = "https://api.avalanche.state.co.us/api/v2/observation_reports"

_forecast_cache: tuple[dict, datetime] | None = None
_avid_cache: tuple[tuple[list, dict], datetime] | None = None
_obs_cache: tuple[dict, datetime] | None = None


# ── map-layer forecast (polygon fill layer) ────────────────────────────────────

@router.get("/forecast")
async def caic_forecast() -> dict:
    """CAIC danger-zone GeoJSON cached 1 hour — used for the polygon fill layer."""
    global _forecast_cache
    if _forecast_cache is not None:
        data, fetched_at = _forecast_cache
        if datetime.utcnow() - fetched_at < _CACHE_TTL:
            return data

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_MAPLAYER_URL, params={"type": "forecast", "center_id": "CAIC"})
    except httpx.RequestError as exc:
        logger.error("avalanche.org unreachable: %s", exc)
        raise HTTPException(502, "avalanche.org unreachable") from exc

    if resp.status_code != 200:
        raise HTTPException(502, f"avalanche.org returned {resp.status_code}")

    raw = resp.json()
    # center_id param is ignored server-side — filter to CAIC zones only.
    features = [f for f in raw.get("features", []) if f.get("properties", {}).get("center_id") == "CAIC"]
    data = {**raw, "features": features}
    _forecast_cache = (data, datetime.utcnow())
    return data


# ── AVID detail (danger rose + problems) ──────────────────────────────────────

async def _fetch_avid() -> tuple[list, dict]:
    """Fetch and cache AVID products + areas GeoJSON (1h TTL)."""
    global _avid_cache
    if _avid_cache is not None:
        result, fetched_at = _avid_cache
        if datetime.utcnow() - fetched_at < _CACHE_TTL:
            return result

    try:
        async with httpx.AsyncClient(timeout=12) as client:
            products_resp, areas_resp = await asyncio.gather(
                client.get(_AVID_URL, params={"_api_proxy_uri": "/products/all?includeExpired=true"}),
                client.get(_AVID_URL, params={
                    "_api_proxy_uri": "/products/all/area?productType=avalancheforecast&includeExpired=true"
                }),
            )
    except httpx.RequestError as exc:
        logger.error("AVID unreachable: %s", exc)
        raise HTTPException(502, "AVID unreachable") from exc

    if products_resp.status_code != 200 or areas_resp.status_code != 200:
        raise HTTPException(502, "AVID returned non-200")

    products = [p for p in products_resp.json() if p.get("type") == "avalancheforecast"]
    areas = areas_resp.json()
    result = (products, areas)
    _avid_cache = (result, datetime.utcnow())
    return result


# ── point-in-polygon (ray casting) ────────────────────────────────────────────

def _ray_cross(lat: float, lng: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]   # lng, lat
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


@router.get("/zone_detail")
async def caic_zone_detail(
    lat: float = Query(..., description="Clicked latitude"),
    lng: float = Query(..., description="Clicked longitude"),
) -> dict:
    """Full danger rose + avalanche problems for the CAIC zone at (lat, lng)."""
    products, areas = await _fetch_avid()

    # Build area_id → geometry + centroid lookup
    area_map: dict[str, dict] = {}
    for feat in areas.get("features", []):
        aid = feat["properties"].get("id")
        if aid:
            area_map[aid] = {
                "geometry": feat["geometry"],
                "centroid": feat["properties"].get("centroid", [0, 0]),
            }

    # Try point-in-polygon first
    for product in products:
        aid = product.get("areaId")
        if aid and aid in area_map:
            if _in_multipolygon(lat, lng, area_map[aid]["geometry"]):
                return _build_zone_detail(product)

    # Fallback: nearest centroid (handles clicks near zone boundaries)
    best_product = None
    best_dist = float("inf")
    for product in products:
        aid = product.get("areaId")
        if aid and aid in area_map:
            d = _centroid_dist(lat, lng, area_map[aid]["centroid"])
            if d < best_dist:
                best_dist = d
                best_product = product

    if best_product is None:
        raise HTTPException(404, "No CAIC zone found")

    return _build_zone_detail(best_product)


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


@router.get("/observations")
async def caic_observations() -> dict:
    """CAIC field observations as GeoJSON — last 7 days, 30-min cache."""
    global _obs_cache
    if _obs_cache is not None:
        data, fetched_at = _obs_cache
        if datetime.utcnow() - fetched_at < _OBS_CACHE_TTL:
            return data

    since = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d")
    params = {
        "r[observed_at_gteq]": since,
        "r[sorts][]": "observed_at desc",
        "per": "500",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_OBS_URL, params=params)
    except httpx.RequestError as exc:
        logger.error("CAIC observations unreachable: %s", exc)
        raise HTTPException(502, "CAIC observations unreachable") from exc

    if resp.status_code != 200:
        raise HTTPException(502, f"CAIC observations returned {resp.status_code}")

    records = resp.json()
    features = [f for r in records if (f := _build_obs_feature(r)) is not None]
    data = {"type": "FeatureCollection", "features": features}
    _obs_cache = (data, datetime.utcnow())
    return data
