"""A fake Strava API for the webhook, disconnect and 7-day refresh tests.

Replaces the shared httpx client in app.services.strava, so the real code
that turns Strava's answers into revoked / valid / gone / unknown is tested.
Also fixtures-in-functions for Strava connections and imported routes.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from geoalchemy2.shape import from_shape
from shapely.geometry import LineString
from tenacity import wait_none

from app import http_retry
from app.models.route import Route
from app.models.strava import StravaConnection
from app.services import strava as strava_svc

TRACK = [(-106.40, 39.10), (-106.39, 39.11), (-106.38, 39.115)]


def encode_polyline(coords: list[tuple[float, float]]) -> str:
    """Google polyline encoding of [(lng, lat), ...] (the inverse of
    strava_svc._decode_polyline)."""
    out, prev_lat, prev_lng = [], 0, 0
    for lng, lat in coords:
        ilat, ilng = round(lat * 1e5), round(lng * 1e5)
        for value in (ilat - prev_lat, ilng - prev_lng):
            v = ~(value << 1) if value < 0 else value << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        prev_lat, prev_lng = ilat, ilng
    return "".join(out)


class FakeStrava:
    """``athlete``: status for GET /athlete. ``activities``: id -> status
    (int) or a detail dict (200). ``refresh``: status for the token refresh.
    ``down``: every call raises a network error. ``calls``: paths asked."""

    def __init__(self) -> None:
        self.athlete = 200
        self.activities: dict[int, int | dict] = {}
        self.refresh = 200
        self.down = False
        self.calls: list[str] = []

    def _resp(self, method: str, url: str, status: int, body: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body or {}, request=httpx.Request(method, url))

    async def get(self, url, headers=None, params=None, timeout=None):
        path = url.removeprefix(strava_svc.STRAVA_API_BASE)
        self.calls.append(f"GET {path}")
        if self.down:
            raise httpx.ConnectError("down", request=httpx.Request("GET", url))
        if path == "/athlete":
            return self._resp("GET", url, self.athlete, {"id": 1})
        if path.startswith("/activities/"):
            v = self.activities.get(int(path.rsplit("/", 1)[1]), 404)
            if isinstance(v, dict):
                return self._resp("GET", url, 200, v)
            return self._resp("GET", url, v)
        return self._resp("GET", url, 404)

    async def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append(f"POST {url}")
        if self.down:
            raise httpx.ConnectError("down", request=httpx.Request("POST", url))
        if url == strava_svc.STRAVA_TOKEN_URL:
            return self._resp("POST", url, self.refresh, {
                "access_token": "new-acc", "refresh_token": "new-ref", "expires_at": 2_000_000_000,
            })
        return self._resp("POST", url, 200)


def install(monkeypatch) -> FakeStrava:
    fake = FakeStrava()
    monkeypatch.setattr(strava_svc._HTTP, "get", fake.get)
    monkeypatch.setattr(strava_svc._HTTP, "post", fake.post)
    # No breaker state from (or into) other tests; no retry back-off waits.
    # (a fresh registry, put back afterwards, so nothing leaks either way)
    monkeypatch.setattr(http_retry, "_BREAKERS", {})
    monkeypatch.setattr(http_retry, "wait_exponential_jitter", lambda **_: wait_none())
    return fake


def connect(db_session, user, athlete_id: int, *, expired: bool = False) -> StravaConnection:
    conn = StravaConnection(
        user_id=user.id, strava_athlete_id=athlete_id, access_token="acc", refresh_token="ref",
        expires_at=1 if expired else 2_000_000_000, scope="activity:read_all", athlete_name="A",
    )
    db_session.add(conn)
    db_session.flush()
    return conn


def make_route(
    db_session, user, *, activity_id: int | None = None, fetched_at: datetime | None = None,
    track: list[tuple[float, float]] = TRACK, name: str = "R",
) -> Route:
    route = Route(
        owner_id=user.id, name=name, region="colorado",
        geom=from_shape(LineString([(x, y, 3000.0) for x, y in track]), srid=4326),
        summary={"distance_m": 1500.0}, samples=[],
        source_strava_id=activity_id,
        strava_fetched_at=fetched_at if fetched_at or activity_id is None else datetime.now(UTC),
    )
    db_session.add(route)
    db_session.flush()
    return route
