"""SNOTEL station data from NRCS AWDB REST API v1.

To swap data source: replace AWDB_BASE and the _fetch_* functions below.
The public contract is fetch_stations_geojson() → GeoJSON FeatureCollection.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.http_retry import call_with_resilience

logger = logging.getLogger("whumpf.snotel")

# ── provider config — swap these to change the data source ────────────────────
AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
_BATCH_SIZE = 40   # triplets per AWDB request (URL length limit)
_CACHE_TTL = timedelta(hours=6)

_HTTP = httpx.AsyncClient(
    headers={"User-Agent": "whumpf/0.1 (backcountry-terrain-app)"},
    follow_redirects=True,
    timeout=60.0,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

_cache: tuple[dict, datetime] | None = None  # (data, fetched_at)
_lock = asyncio.Lock()


# ── color coding (% of normal) ─────────────────────────────────────────────────

def _pct_color(pct: float | None) -> str:
    """CalTopo-inspired ramp: red = drought, blue = exceptional snowpack."""
    if pct is None:
        return "#888888"
    if pct < 50:
        return "#d7191c"
    if pct < 75:
        return "#f4820a"
    if pct < 100:
        return "#ffeb00"
    if pct < 125:
        return "#78c679"
    return "#1a9641"


# ── AWDB fetch helpers ─────────────────────────────────────────────────────────

async def _fetch_stations() -> list[dict]:
    async def _do() -> list[dict]:
        r = await _HTTP.get(
            f"{AWDB_BASE}/stations",
            params={
                "maxResults": 1000,
                "activeOnly": True,
                "stateCode": "CO",
                "networkCodes": "SNTL",
            },
        )
        r.raise_for_status()
        # AWDB ignores stateCode/networkCodes params — filter client-side
        return [s for s in r.json() if s.get("stateCode") == "CO" and s.get("networkCode") == "SNTL"]

    return await call_with_resilience("awdb", _do)


async def _fetch_data_batch(triplets: list[str], today: str, begin: str) -> list[dict]:
    """Fetch current values + median normals in one request using centralTendencyType=MEDIAN."""
    async def _do() -> list[dict]:
        r = await _HTTP.get(
            f"{AWDB_BASE}/data",
            params={
                "stationTriplets": ",".join(triplets),
                "elements": "WTEQ,SNWD,TOBS",
                "duration": "DAILY",
                "centralTendencyType": "MEDIAN",
                "beginDate": begin,
                "endDate": today,
            },
        )
        r.raise_for_status()
        return r.json()

    return await call_with_resilience("awdb", _do)


# ── response parsing ───────────────────────────────────────────────────────────

def _extract_latest(station_data: list[dict], element_cd: str) -> tuple[float | None, float | None]:
    """Return (latest_value, median_normal) for an element from AWDB data response."""
    for item in station_data:
        elem = item.get("stationElement") or {}
        if elem.get("elementCode") != element_cd:
            continue
        vals = item.get("values") or []
        if not vals:
            continue
        value: float | None = None
        median: float | None = None
        for entry in reversed(vals):
            raw = entry.get("value")
            if raw is not None:
                try:
                    v = float(raw)
                    if v != -9999.0:
                        value = v
                        raw_med = entry.get("median")
                        if raw_med is not None:
                            try:
                                m = float(raw_med)
                                median = m if m != -9999.0 else None
                            except (TypeError, ValueError):
                                pass
                        break
                except (TypeError, ValueError):
                    pass
        return value, median
    return None, None


def _build_data_index(results: list[dict]) -> dict[str, list[dict]]:
    return {r["stationTriplet"]: r.get("data") or [] for r in results}


# ── fetch + cache internals ───────────────────────────────────────────────────

async def _do_fetch() -> dict:
    """Execute the full AWDB fetch and update the module cache."""
    global _cache
    today = date.today().isoformat()
    begin = (date.today() - timedelta(days=7)).isoformat()

    stations = await _fetch_stations()
    triplets = [s["stationTriplet"] for s in stations]
    batches = [triplets[i : i + _BATCH_SIZE] for i in range(0, len(triplets), _BATCH_SIZE)]

    data_batches = await asyncio.gather(
        *[_fetch_data_batch(b, today, begin) for b in batches],
        return_exceptions=True,
    )

    data_results: list[dict] = []
    for batch in data_batches:
        if isinstance(batch, Exception):
            logger.warning("SNOTEL data batch failed: %s", batch)
        else:
            data_results.extend(batch)

    data_idx = _build_data_index(data_results)

    features: list[dict] = []
    for s in stations:
        triplet = s["stationTriplet"]
        d = data_idx.get(triplet, [])

        swe, swe_median = _extract_latest(d, "WTEQ")
        depth, _        = _extract_latest(d, "SNWD")
        temp, _         = _extract_latest(d, "TOBS")

        pct = round(swe / swe_median * 100, 1) if (swe is not None and swe_median and swe_median > 0) else None

        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [s["longitude"], s["latitude"]]},
            "properties": {
                "id": triplet,
                "name": s["name"],
                "elevation_ft": s.get("elevation"),
                "swe_in": swe,
                "snow_depth_in": depth,
                "temp_f": temp,
                "swe_pct_normal": pct,
                "color": _pct_color(pct),
                "label": f'{swe:.1f}"' if swe is not None else "—",
                "updated": today,
            },
        })

    geojson: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    _cache = (geojson, datetime.utcnow())
    logger.info("SNOTEL: %d stations loaded, cached %dh", len(features), int(_CACHE_TTL.total_seconds() / 3600))
    return geojson


async def _bg_refresh() -> None:
    """Background refresh — no-op if a fetch is already running."""
    if _lock.locked():
        return
    async with _lock:
        # Re-check inside the lock: another task may have just refreshed.
        if _cache is not None:
            _, fetched_at = _cache
            if datetime.utcnow() - fetched_at < _CACHE_TTL:
                return
        try:
            await _do_fetch()
        except Exception as exc:
            logger.warning("SNOTEL background refresh failed: %s", exc)


# ── public interface ───────────────────────────────────────────────────────────

async def fetch_stations_geojson() -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of all active Colorado SNOTEL stations.

    Returns immediately from cache when available (even if stale — a background
    refresh is triggered automatically). Only blocks on the very first call when
    there is no cached data yet.
    """
    global _cache

    if _cache is not None:
        data, fetched_at = _cache
        if datetime.utcnow() - fetched_at < _CACHE_TTL:
            return data
        # Stale: serve immediately and refresh in background.
        asyncio.ensure_future(_bg_refresh())
        return data

    # No data yet — cold start, must wait for first fetch.
    async with _lock:
        if _cache is not None:   # another coroutine just finished while we waited
            return _cache[0]
        return await _do_fetch()
