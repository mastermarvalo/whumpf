"""Saved-route persistence — owner-scoped polylines with a cached terrain profile.

Phase A: create / list / view / edit / delete, owner-only.
Phase B: share-link create/revoke, token-scoped view, and clone.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, Field
from pyproj import Geod
from shapely.geometry import LineString, mapping
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.access import assert_can_edit, assert_can_view
from app.auth.dependencies import get_current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.models.route import Route, Visibility
from app.models.route_share import RouteShare
from app.models.strava import StravaConnection
from app.models.user import User
from app.regions import validate_region
from app.routers.terrain import _MAX_PROFILE_DISTANCE_M
from app.services import strava as strava_svc
from app.services.cog_sampler import sample_polyline
from app.services.profile_summary import _aspect_bucket, summarise

router = APIRouter(prefix="/routes", tags=["routes"])
logger = logging.getLogger("whumpf.routes")

_GEOD = Geod(ellps="WGS84")

# Total sample-point budget for a saved route's cached profile. A few hundred
# points is plenty to render a chart/histograms and keeps the JSONB small.
_ROUTE_SAMPLE_BUDGET = 192


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class GeoJSONLineString(BaseModel):
    type: Literal["LineString"]
    # Each coordinate is [lng, lat] (an optional 3rd Z element is ignored — we
    # recompute Z from the DEM).
    coordinates: list[list[float]]


class RouteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    region: str
    geometry: GeoJSONLineString
    notes: str = ""


class RouteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    notes: str | None = None
    visibility: Visibility | None = None


class RouteRead(BaseModel):
    id: int
    owner_id: int
    name: str
    notes: str
    region: str
    visibility: Visibility
    geometry: dict
    summary: dict
    samples: list
    created_at: datetime
    updated_at: datetime


class RouteListItem(BaseModel):
    """Lightweight list entry — omits the per-point ``samples`` payload."""

    id: int
    owner_id: int
    name: str
    notes: str
    region: str
    visibility: Visibility
    geometry: dict
    summary: dict
    created_at: datetime
    updated_at: datetime


class ShareOut(BaseModel):
    token: str


SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _geometry_dict(route: Route) -> dict:
    """PostGIS geometry → GeoJSON dict (coordinates include Z)."""
    return mapping(to_shape(route.geom))


def _to_read(route: Route) -> RouteRead:
    return RouteRead(
        id=route.id,
        owner_id=route.owner_id,
        name=route.name,
        notes=route.notes,
        region=route.region,
        visibility=route.visibility,
        geometry=_geometry_dict(route),
        summary=route.summary,
        samples=route.samples,
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


def _to_list_item(route: Route) -> RouteListItem:
    return RouteListItem(
        id=route.id,
        owner_id=route.owner_id,
        name=route.name,
        notes=route.notes,
        region=route.region,
        visibility=route.visibility,
        geometry=_geometry_dict(route),
        summary=route.summary,
        created_at=route.created_at,
        updated_at=route.updated_at,
    )


def _vertex_distances(vertices: list[tuple[float, float]]) -> list[float]:
    """Cumulative geodetic distance (m) at each vertex."""
    dists = [0.0]
    for (lng0, lat0), (lng1, lat1) in zip(vertices, vertices[1:]):
        _, _, m = _GEOD.inv(lng0, lat0, lng1, lat1)
        dists.append(dists[-1] + m)
    return dists


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post("", response_model=RouteRead, status_code=201)
def create_route(
    payload: RouteCreate,
    session: SessionDep,
    user: UserDep,
    settings: SettingsDep,
) -> RouteRead:
    validate_region(payload.region)
    vertices = [(float(c[0]), float(c[1])) for c in payload.geometry.coordinates]
    route = _build_route(
        session, user,
        name=payload.name, region=payload.region,
        vertices=vertices, settings=settings, notes=payload.notes,
    )
    return _to_read(route)


def _build_route(
    session: Session,
    user: User,
    *,
    name: str,
    region: str,
    vertices: list[tuple[float, float]],
    settings: Settings,
    notes: str = "",
    source_strava_id: int | None = None,
) -> Route:
    """Sample terrain for a polyline, compute the summary, and persist a new
    private route owned by ``user``. Shared by the draw and Strava-import paths.

    Caller is responsible for validating ``region`` against the registry.
    """
    if len(vertices) < 2:
        raise HTTPException(400, "A route needs at least two vertices")

    vdists = _vertex_distances(vertices)
    total_m = vdists[-1]
    if total_m > _MAX_PROFILE_DISTANCE_M:
        raise HTTPException(
            400,
            f"Route distance {total_m/1000:.0f} km exceeds the "
            f"{_MAX_PROFILE_DISTANCE_M/1000:.0f} km limit",
        )

    try:
        samples = sample_polyline(vertices, region, settings, n_total=_ROUTE_SAMPLE_BUDGET)
    except Exception as exc:
        logger.error("route terrain sampling failed: %s", exc)
        raise HTTPException(502, f"COG read failed: {exc}") from exc

    # Z per vertex: interpolate elevation from the sampled profile by cumulative
    # distance (avoids a second COG read just for the vertices).
    sd = [s.distance_m for s in samples if s.elevation_m is not None]
    se = [s.elevation_m for s in samples if s.elevation_m is not None]
    vz = np.interp(vdists, sd, se).tolist() if sd else [0.0] * len(vertices)
    line = LineString([(lng, lat, z) for (lng, lat), z in zip(vertices, vz)])

    summary = {"distance_m": round(samples[-1].distance_m, 1), **summarise(samples)}
    samples_json = [
        {
            "distance_m": round(s.distance_m, 1),
            "elevation_m": round(s.elevation_m, 1) if s.elevation_m is not None else None,
            "slope_deg": round(s.slope_deg, 2) if s.slope_deg is not None else None,
            "aspect_deg": round(s.aspect_deg, 1) if s.aspect_deg is not None else None,
            "aspect": _aspect_bucket(s.aspect_deg),
        }
        for s in samples
    ]

    route = Route(
        owner_id=user.id,
        name=name,
        notes=notes,
        region=region,
        geom=from_shape(line, srid=4326),
        summary=summary,
        samples=samples_json,
        visibility=Visibility.private,
        source_strava_id=source_strava_id,
    )
    session.add(route)
    session.commit()
    session.refresh(route)
    return route


@router.post("/import/strava/{activity_id}", response_model=RouteRead, status_code=201)
async def import_strava_route(
    activity_id: int,
    session: SessionDep,
    user: UserDep,
    settings: SettingsDep,
    response: Response,
    region: str = Query(..., description="Region to sample terrain against"),
) -> RouteRead:
    """Import a Strava activity's GPS track as a full route, sampling terrain
    server-side. Uses the activity's detailed polyline (falls back to the
    summary polyline) so the saved route is full-resolution.

    Idempotent per owner: re-importing the same activity returns the existing
    route (200) instead of creating a duplicate."""
    validate_region(region)

    # Dedupe: this owner already imported this activity → return it as-is.
    existing = session.scalars(
        select(Route).where(
            Route.owner_id == user.id,
            Route.source_strava_id == activity_id,
        )
    ).first()
    if existing is not None:
        response.status_code = 200
        return _to_read(existing)

    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if not conn:
        raise HTTPException(404, "Strava not connected")

    try:
        access_token = await strava_svc.refresh_token(conn, session)
        detail = await strava_svc.fetch_activity_detail(access_token, activity_id)
    except Exception as exc:
        logger.error("strava import fetch failed: %s", exc)
        raise HTTPException(502, f"Strava unavailable: {exc}") from exc

    map_ = detail.get("map") or {}
    encoded = map_.get("polyline") or map_.get("summary_polyline") or ""
    coords = strava_svc._decode_polyline(encoded) if encoded else []
    if len(coords) < 2:
        raise HTTPException(400, "Activity has no GPS track to import")
    vertices = [(float(c[0]), float(c[1])) for c in coords]
    name = (detail.get("name") or "").strip() or "Imported Strava route"

    # Terrain sampling + DB write are blocking; keep them off the event loop.
    def _do() -> RouteRead:
        route = _build_route(
            session, user,
            name=name, region=region, vertices=vertices,
            settings=settings, notes="Imported from Strava",
            source_strava_id=activity_id,
        )
        return _to_read(route)

    return await run_in_threadpool(_do)


@router.get("", response_model=list[RouteListItem])
def list_routes(session: SessionDep, user: UserDep) -> list[RouteListItem]:
    # Phase A: caller's own routes only. (Phase B adds routes shared to them.)
    rows = session.scalars(
        select(Route).where(Route.owner_id == user.id).order_by(Route.created_at.desc())
    ).all()
    return [_to_list_item(r) for r in rows]


def _get_or_404(session: Session, route_id: int) -> Route:
    route = session.get(Route, route_id)
    if route is None:
        raise HTTPException(404, "Route not found")
    return route


@router.get("/{route_id}", response_model=RouteRead)
def get_route(
    route_id: int,
    session: SessionDep,
    user: UserDep,
    token: str | None = Query(default=None, description="Share token for non-owner access"),
) -> RouteRead:
    route = _get_or_404(session, route_id)
    assert_can_view(user, route, session=session, token=token)
    return _to_read(route)


@router.patch("/{route_id}", response_model=RouteRead)
def update_route(
    route_id: int, payload: RouteUpdate, session: SessionDep, user: UserDep,
) -> RouteRead:
    route = _get_or_404(session, route_id)
    assert_can_edit(user, route)
    if payload.name is not None:
        route.name = payload.name
    if payload.notes is not None:
        route.notes = payload.notes
    if payload.visibility is not None:
        route.visibility = payload.visibility
    session.commit()
    session.refresh(route)
    return _to_read(route)


@router.delete("/{route_id}", status_code=204)
def delete_route(route_id: int, session: SessionDep, user: UserDep) -> None:
    route = _get_or_404(session, route_id)
    assert_can_edit(user, route)
    session.delete(route)
    session.commit()


# --------------------------------------------------------------------------- #
# Sharing (Phase B)
# --------------------------------------------------------------------------- #
def _new_share_token() -> str:
    """Cryptographically secure URL-safe share token (~43 chars)."""
    return secrets.token_urlsafe(32)


def _active_share(session: Session, route_id: int) -> RouteShare | None:
    return session.scalars(
        select(RouteShare).where(
            RouteShare.route_id == route_id,
            RouteShare.revoked_at.is_(None),
        )
    ).first()


def _clone_route(session: Session, user: User, source: Route) -> Route:
    """Deep-copy a route into ``user``'s account as a new private route.

    Copies the stored geometry/summary/samples verbatim — no terrain re-sampling.
    """
    clone = Route(
        owner_id=user.id,
        name=f"{source.name} (copy)",
        notes=source.notes,
        region=source.region,
        geom=from_shape(to_shape(source.geom), srid=4326),
        summary=source.summary,
        samples=source.samples,
        visibility=Visibility.private,
        source_strava_id=None,
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)
    return clone


@router.post("/{route_id}/share", response_model=ShareOut)
def share_route(
    route_id: int, session: SessionDep, user: UserDep, response: Response,
) -> ShareOut:
    """Create (or return the existing) share link for a route. Owner only.

    One active link per route: a second call returns the same token (200).
    Generating a link flips visibility to `unlisted` as a cosmetic label —
    access is governed by the token, not the visibility value."""
    route = _get_or_404(session, route_id)
    assert_can_edit(user, route)

    existing = _active_share(session, route.id)
    if existing is not None:
        response.status_code = 200
        return ShareOut(token=existing.token)

    share = RouteShare(
        route_id=route.id, token=_new_share_token(), created_by_id=user.id,
    )
    session.add(share)
    if route.visibility == Visibility.private:
        route.visibility = Visibility.unlisted
    session.commit()
    session.refresh(share)
    response.status_code = 201
    return ShareOut(token=share.token)


@router.delete("/{route_id}/share/{token}", status_code=204)
def revoke_share(
    route_id: int, token: str, session: SessionDep, user: UserDep,
) -> None:
    """Revoke a share token. Owner only. Resets visibility to private when no
    active links remain."""
    route = _get_or_404(session, route_id)
    assert_can_edit(user, route)

    share = session.scalars(
        select(RouteShare).where(
            RouteShare.route_id == route.id,
            RouteShare.token == token,
            RouteShare.revoked_at.is_(None),
        )
    ).first()
    if share is None:
        raise HTTPException(404, "Share link not found")

    share.revoked_at = datetime.now(timezone.utc)
    # Any *other* active links left? (Query excludes this token explicitly so it's
    # correct without relying on the unflushed revoked_at above.)
    others = session.scalars(
        select(RouteShare).where(
            RouteShare.route_id == route.id,
            RouteShare.revoked_at.is_(None),
            RouteShare.token != token,
        )
    ).first()
    if others is None and route.visibility == Visibility.unlisted:
        route.visibility = Visibility.private
    session.commit()


@router.post("/{route_id}/clone", response_model=RouteRead, status_code=201)
def clone_route(
    route_id: int,
    session: SessionDep,
    user: UserDep,
    token: str | None = Query(default=None, description="Share token if not the owner"),
) -> RouteRead:
    """Clone a route the caller can view (own, or via a valid share token) into
    their account as a new private route."""
    route = _get_or_404(session, route_id)
    assert_can_view(user, route, session=session, token=token)
    clone = _clone_route(session, user, route)
    return _to_read(clone)
