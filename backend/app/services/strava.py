"""Strava API client: OAuth exchange, token refresh, activity fetch."""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.http_retry import call_with_resilience
from app.models.strava import StravaConnection

logger = logging.getLogger("whumpf.strava")

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = "https://www.strava.com/api/v3"
SCOPE = "activity:read_all"

# Persistent client — connection pooling across all Strava calls. Was previously
# a fresh httpx.AsyncClient() per call which paid a TLS handshake every time.
_HTTP = httpx.AsyncClient(
    timeout=15.0,
    limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
)

# Sport types and their display colors for GeoJSON properties
SPORT_COLORS: dict[str, str] = {
    "BackcountrySki": "#00bfff",
    "AlpineSki": "#87ceeb",
    "NordicSki": "#00ced1",
    "Snowshoe": "#b0e0e6",
    "Hike": "#2ecc71",
    "Walk": "#27ae60",
    "Run": "#e74c3c",
    "TrailRun": "#c0392b",
    "Ride": "#3498db",
    "GravelRide": "#2980b9",
    "MountainBikeRide": "#8e44ad",
}
DEFAULT_COLOR = "#95a5a6"


def get_authorize_url(state: str) -> str:
    s = get_settings()
    params = {
        "client_id": s.strava_client_id,
        "redirect_uri": s.strava_redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{STRAVA_AUTH_URL}?{query}"


async def exchange_code(code: str) -> dict:
    s = get_settings()

    async def _do() -> dict:
        r = await _HTTP.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": s.strava_client_id,
                "client_secret": s.strava_client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    return await call_with_resilience("strava", _do)


async def refresh_token(conn: StravaConnection, session: Session) -> str:
    """Refresh and persist a new access token if the current one is expired."""
    if conn.expires_at > int(time.time()) + 60:
        return conn.access_token

    s = get_settings()

    async def _do() -> dict:
        r = await _HTTP.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": s.strava_client_id,
                "client_secret": s.strava_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": conn.refresh_token,
            },
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    data = await call_with_resilience("strava", _do)

    conn.access_token = data["access_token"]
    conn.refresh_token = data["refresh_token"]
    conn.expires_at = data["expires_at"]
    session.commit()
    logger.info("Strava token refreshed for athlete %d", conn.strava_athlete_id)
    return conn.access_token


async def fetch_activities(access_token: str, per_page: int = 100) -> list[dict]:
    async def _do() -> list[dict]:
        r = await _HTTP.get(
            f"{STRAVA_API_BASE}/athlete/activities",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"per_page": per_page, "page": 1},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    return await call_with_resilience("strava", _do)


def _decode_polyline(encoded: str) -> list[list[float]]:
    """Decode Google encoded polyline → [[lng, lat], ...] for GeoJSON."""
    coords: list[list[float]] = []
    index = lat = lng = 0
    while index < len(encoded):
        for is_lat in (True, False):
            result = shift = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            value = ~(result >> 1) if result & 1 else result >> 1
            if is_lat:
                lat += value
            else:
                lng += value
        coords.append([lng / 1e5, lat / 1e5])
    return coords


def activities_to_geojson(activities: list[dict]) -> dict:
    features = []
    for act in activities:
        polyline = (act.get("map") or {}).get("summary_polyline") or ""
        if not polyline:
            continue
        coords = _decode_polyline(polyline)
        if len(coords) < 2:
            continue
        sport = act.get("sport_type") or act.get("type") or "Other"
        primary = (act.get("photos") or {}).get("primary") or {}
        photo_urls = primary.get("urls") or {}
        photo_url = photo_urls.get("600") or photo_urls.get("100") or None
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "id": act.get("id"),
                "name": act.get("name", ""),
                "sport_type": sport,
                "color": SPORT_COLORS.get(sport, DEFAULT_COLOR),
                "distance_m": act.get("distance", 0),
                "elapsed_time_s": act.get("elapsed_time", 0),
                "total_elevation_gain_m": act.get("total_elevation_gain", 0),
                "start_date": act.get("start_date", ""),
                "photo_url": photo_url,
            },
        })
    return {"type": "FeatureCollection", "features": features}


async def fetch_activity_detail(access_token: str, activity_id: int) -> dict:
    async def _do() -> dict:
        r = await _HTTP.get(
            f"{STRAVA_API_BASE}/activities/{activity_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    return await call_with_resilience("strava", _do)


async def deauthorize(access_token: str) -> None:
    """Revoke this access token on Strava's side. 401 is treated as success
    (already revoked); other non-2xx status codes propagate so the caller can
    decide whether to log and continue.
    """
    async def _do() -> None:
        r = await _HTTP.post(
            "https://www.strava.com/oauth/deauthorize",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        if r.status_code == 401:
            return  # already revoked
        r.raise_for_status()

    await call_with_resilience("strava", _do)
