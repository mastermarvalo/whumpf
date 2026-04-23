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

logger = logging.getLogger("whumpf.snotel")

# ── provider config — swap these to change the data source ────────────────────
AWDB_BASE = "https://wcc.sc.egov.usda.gov/awdbRestApi/services/v1"
_BATCH_SIZE = 40   # triplets per AWDB request (URL length limit)
_CACHE_TTL_H = 6   # SNOTEL updates daily; cache aggressively

_cache: tuple[dict, datetime] | None = None


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

async def _fetch_stations(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(
        f"{AWDB_BASE}/stations",
        params={
            "maxResults": 1000,
            "activeOnly": True,
            "stateCode": "CO",
            "networkCodes": "SNTL",
        },
        timeout=30,
    )
    r.raise_for_status()
    # AWDB ignores stateCode/networkCodes params — filter client-side
    return [s for s in r.json() if s.get("stateCode") == "CO" and s.get("networkCode") == "SNTL"]


async def _fetch_data_batch(
    client: httpx.AsyncClient, triplets: list[str], today: str, begin: str
) -> list[dict]:
    """Fetch current values + median normals in one request using centralTendencyType=MEDIAN."""
    r = await client.get(
        f"{AWDB_BASE}/data",
        params={
            "stationTriplets": ",".join(triplets),
            "elements": "WTEQ,SNWD,TOBS",
            "duration": "DAILY",
            "centralTendencyType": "MEDIAN",
            "beginDate": begin,
            "endDate": today,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


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
        # Take the last non-null value (most recent date)
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


# ── public interface ───────────────────────────────────────────────────────────

async def fetch_stations_geojson() -> dict[str, Any]:
    """Return a GeoJSON FeatureCollection of all active Colorado SNOTEL stations."""
    global _cache

    now = datetime.utcnow()
    if _cache and now < _cache[1]:
        return _cache[0]

    today = date.today().isoformat()
    begin = (date.today() - timedelta(days=7)).isoformat()  # 7-day window ensures we get the latest reading

    async with httpx.AsyncClient(
        headers={"User-Agent": "whumpf/0.1 (backcountry-terrain-app)"},
        follow_redirects=True,
    ) as client:
        stations = await _fetch_stations(client)
        triplets = [s["stationTriplet"] for s in stations]

        batches = [triplets[i: i + _BATCH_SIZE] for i in range(0, len(triplets), _BATCH_SIZE)]

        data_batches = await asyncio.gather(
            *[_fetch_data_batch(client, b, today, begin) for b in batches],
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

        swe, swe_median   = _extract_latest(d, "WTEQ")
        depth, _          = _extract_latest(d, "SNWD")
        temp, _           = _extract_latest(d, "TOBS")

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
    _cache = (geojson, now + timedelta(hours=_CACHE_TTL_H))
    logger.info("SNOTEL: %d stations loaded, cached %dh", len(features), _CACHE_TTL_H)
    return geojson
