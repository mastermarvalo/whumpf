"""Trips — dated plans bundling route(s), a frozen CAIC forecast, a party, and
planning waypoints. Access is membership-based; edits are owner-only.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, Field
from shapely.geometry import Point, mapping
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.access import assert_can_view_trip, assert_trip_owner
from app.auth.dependencies import get_current_user
from app.db import get_session
from app.models.route import Route
from app.models.trip import Trip
from app.models.trip_member import MemberRole, MemberStatus, TripMember
from app.models.trip_route import TripRoute
from app.models.user import User
from app.models.waypoint import Waypoint, WaypointKind
from app.regions import validate_region
from app.routers.avalanche import get_zone_detail_for_point
from app.routers.routes import RouteRead, _to_read
from app.services.email import send_trip_invite

router = APIRouter(prefix="/trips", tags=["trips"])
logger = logging.getLogger("whumpf.trips")

SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[User, Depends(get_current_user)]


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class TripRouteRead(RouteRead):
    trip_route_id: int


class TripRouteIn(BaseModel):
    route_id: int
    day: int = Field(default=1, ge=1)


class TripDayIn(BaseModel):
    route_ids: list[int] = Field(default_factory=list)


class TripCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    date: dt.date
    region: str
    # One entry per day (>=1). Each day lists the routes assigned to it.
    days: list[TripDayIn] = Field(min_length=1)
    notes: str = ""


class TripUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    date: dt.date | None = None
    notes: str | None = None


class InviteIn(BaseModel):
    email: str


class RespondIn(BaseModel):
    action: Literal["accept", "decline"]


class MemberOut(BaseModel):
    id: int
    user_id: int | None
    email: str
    status: MemberStatus
    role: MemberRole


class WaypointIn(BaseModel):
    lng: float
    lat: float
    elevation_m: float = 0.0
    kind: WaypointKind = WaypointKind.other
    label: str = ""
    notes: str = ""


class WaypointUpdate(BaseModel):
    lng: float | None = None
    lat: float | None = None
    elevation_m: float | None = None
    kind: WaypointKind | None = None
    label: str | None = None
    notes: str | None = None


class WaypointOut(BaseModel):
    id: int
    trip_id: int
    geometry: dict
    kind: WaypointKind
    label: str
    notes: str
    created_by_id: int


class TripDayOut(BaseModel):
    day: int            # 1-based
    date: dt.date       # start date + (day - 1)
    routes: list[TripRouteRead]


class TripListItem(BaseModel):
    id: int
    owner_id: int
    name: str
    date: dt.date
    num_days: int
    region: str
    caic_zone: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class TripDetail(TripListItem):
    notes: str
    forecast_snapshot: dict | None
    days: list[TripDayOut]
    waypoints: list[WaypointOut]
    members: list[MemberOut]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _get_trip_or_404(session: Session, trip_id: int) -> Trip:
    trip = session.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(404, "Trip not found")
    return trip


def _waypoint_out(wp: Waypoint) -> WaypointOut:
    return WaypointOut(
        id=wp.id, trip_id=wp.trip_id, geometry=mapping(to_shape(wp.geom)),
        kind=wp.kind, label=wp.label, notes=wp.notes, created_by_id=wp.created_by_id,
    )


def _trip_days(session: Session, trip: Trip) -> list[TripDayOut]:
    """Routes grouped by day (1..num_days), each with its calendar date."""
    rows = session.scalars(
        select(TripRoute)
        .where(TripRoute.trip_id == trip.id)
        .order_by(TripRoute.day, TripRoute.ordering)
    ).all()
    by_day: dict[int, list[TripRouteRead]] = {}
    for tr in rows:
        r = session.get(Route, tr.route_id)
        if r is not None:
            by_day.setdefault(tr.day, []).append(
                TripRouteRead(trip_route_id=tr.id, **_to_read(r).model_dump())
            )
    return [
        TripDayOut(
            day=d,
            date=trip.date + dt.timedelta(days=d - 1),
            routes=by_day.get(d, []),
        )
        for d in range(1, max(trip.num_days, 1) + 1)
    ]


def _first_route(session: Session, trip_id: int) -> Route | None:
    tr = session.scalars(
        select(TripRoute)
        .where(TripRoute.trip_id == trip_id)
        .order_by(TripRoute.day, TripRoute.ordering)
    ).first()
    return session.get(Route, tr.route_id) if tr else None


def _members(session: Session, trip_id: int) -> list[MemberOut]:
    rows = session.scalars(
        select(TripMember).where(TripMember.trip_id == trip_id).order_by(TripMember.id)
    ).all()
    return [
        MemberOut(
            id=m.id, user_id=m.user_id, email=m.invited_email,
            status=m.status, role=m.role,
        )
        for m in rows
    ]


def _to_list_item(trip: Trip) -> TripListItem:
    return TripListItem(
        id=trip.id, owner_id=trip.owner_id, name=trip.name, date=trip.date,
        num_days=trip.num_days, region=trip.region, caic_zone=trip.caic_zone,
        created_at=trip.created_at, updated_at=trip.updated_at,
    )


def _to_detail(session: Session, trip: Trip) -> TripDetail:
    return TripDetail(
        **_to_list_item(trip).model_dump(),
        notes=trip.notes,
        forecast_snapshot=trip.forecast_snapshot,
        days=_trip_days(session, trip),
        waypoints=[
            _waypoint_out(w)
            for w in session.scalars(
                select(Waypoint).where(Waypoint.trip_id == trip.id).order_by(Waypoint.id)
            ).all()
        ],
        members=_members(session, trip.id),
    )


def _find_invite(session: Session, trip_id: int, user: User) -> TripMember | None:
    """The caller's invited membership for a trip (by user_id or matching email)."""
    return session.scalars(
        select(TripMember).where(
            TripMember.trip_id == trip_id,
            TripMember.status == MemberStatus.invited,
            or_(TripMember.user_id == user.id, TripMember.invited_email == user.email),
        )
    ).first()


# --------------------------------------------------------------------------- #
# Trip CRUD
# --------------------------------------------------------------------------- #
@router.post("", response_model=TripDetail, status_code=201)
async def create_trip(payload: TripCreate, session: SessionDep, user: UserDep) -> TripDetail:
    validate_region(payload.region)

    # Validate every referenced route is owned by the caller.
    all_ids = [rid for day in payload.days for rid in day.route_ids]
    by_id: dict[int, Route] = {}
    if all_ids:
        found = session.scalars(select(Route).where(Route.id.in_(all_ids))).all()
        by_id = {r.id: r for r in found}
        for rid in all_ids:
            r = by_id.get(rid)
            if r is None or r.owner_id != user.id:
                raise HTTPException(400, f"Route {rid} not found or not yours")

    trip = Trip(
        owner_id=user.id, name=payload.name, date=payload.date,
        num_days=len(payload.days), region=payload.region, notes=payload.notes,
    )

    # Freeze the CAIC forecast for the zone containing the first route's start.
    if all_ids:
        coords = list(to_shape(by_id[all_ids[0]].geom).coords)
        if coords:
            lng, lat = coords[0][0], coords[0][1]
            try:
                detail = await get_zone_detail_for_point(lat, lng)
            except Exception as exc:
                logger.warning("forecast snapshot fetch failed: %s", exc)
                detail = None
            if detail:
                trip.caic_zone = detail.get("zone")
                trip.forecast_snapshot = detail

    session.add(trip)
    session.flush()  # assign trip.id

    for day_idx, day in enumerate(payload.days, start=1):
        for ordering, rid in enumerate(day.route_ids):
            session.add(TripRoute(trip_id=trip.id, route_id=rid, day=day_idx, ordering=ordering))
    session.add(TripMember(
        trip_id=trip.id, user_id=user.id, invited_email=user.email,
        status=MemberStatus.accepted, role=MemberRole.owner,
    ))
    session.commit()
    session.refresh(trip)
    return _to_detail(session, trip)


@router.get("", response_model=list[TripListItem])
def list_trips(session: SessionDep, user: UserDep) -> list[TripListItem]:
    trip_ids = session.scalars(
        select(TripMember.trip_id).where(
            TripMember.user_id == user.id, TripMember.status == MemberStatus.accepted
        )
    ).all()
    if not trip_ids:
        return []
    rows = session.scalars(
        select(Trip).where(Trip.id.in_(trip_ids)).order_by(Trip.date.desc())
    ).all()
    return [_to_list_item(t) for t in rows]


@router.get("/invites", response_model=list[TripListItem])
def list_invites(session: SessionDep, user: UserDep) -> list[TripListItem]:
    """Trips the caller has a pending invite to (by user_id or matching email)."""
    member_rows = session.scalars(
        select(TripMember).where(
            TripMember.status == MemberStatus.invited,
            or_(TripMember.user_id == user.id, TripMember.invited_email == user.email),
        )
    ).all()
    trip_ids = {m.trip_id for m in member_rows}
    if not trip_ids:
        return []
    rows = session.scalars(select(Trip).where(Trip.id.in_(trip_ids))).all()
    return [_to_list_item(t) for t in rows]


@router.get("/{trip_id}", response_model=TripDetail)
def get_trip(trip_id: int, session: SessionDep, user: UserDep) -> TripDetail:
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)
    return _to_detail(session, trip)


@router.patch("/{trip_id}", response_model=TripDetail)
def update_trip(
    trip_id: int, payload: TripUpdate, session: SessionDep, user: UserDep,
) -> TripDetail:
    trip = _get_trip_or_404(session, trip_id)
    assert_trip_owner(user, trip)
    if payload.name is not None:
        trip.name = payload.name
    if payload.date is not None:
        trip.date = payload.date
    if payload.notes is not None:
        trip.notes = payload.notes
    session.commit()
    session.refresh(trip)
    return _to_detail(session, trip)


@router.delete("/{trip_id}", status_code=204)
def delete_trip(trip_id: int, session: SessionDep, user: UserDep) -> None:
    trip = _get_trip_or_404(session, trip_id)
    assert_trip_owner(user, trip)
    # No FK cascade configured — remove children explicitly.
    for model in (Waypoint, TripMember, TripRoute):
        for row in session.scalars(select(model).where(model.trip_id == trip.id)).all():
            session.delete(row)
    session.delete(trip)
    session.commit()


# --------------------------------------------------------------------------- #
# Party members
# --------------------------------------------------------------------------- #
@router.post("/{trip_id}/members", response_model=MemberOut)
async def invite_member(
    trip_id: int, payload: InviteIn, session: SessionDep, user: UserDep, response: Response,
) -> MemberOut:
    trip = _get_trip_or_404(session, trip_id)
    assert_trip_owner(user, trip)
    email = payload.email.lower().strip()
    if "@" not in email:
        raise HTTPException(400, "Invalid email address")

    invitee = session.scalars(select(User).where(User.email == email)).first()

    existing = session.scalars(
        select(TripMember).where(
            TripMember.trip_id == trip.id, TripMember.invited_email == email
        )
    ).first()
    if existing is not None:
        response.status_code = 200
        return MemberOut(
            id=existing.id, user_id=existing.user_id, email=existing.invited_email,
            status=existing.status, role=existing.role,
        )

    member = TripMember(
        trip_id=trip.id,
        user_id=invitee.id if invitee else None,
        invited_email=email,
        status=MemberStatus.invited,
        role=MemberRole.member,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    try:
        await send_trip_invite(
            to=email, trip_name=trip.name, inviter_email=user.email, trip_id=trip.id,
        )
    except Exception as exc:
        logger.warning("Trip-invite email failed for %s: %s", email, exc)
    response.status_code = 201
    return MemberOut(
        id=member.id, user_id=member.user_id, email=member.invited_email,
        status=member.status, role=member.role,
    )


@router.post("/{trip_id}/members/respond", status_code=204)
def respond_invite(
    trip_id: int, payload: RespondIn, session: SessionDep, user: UserDep,
) -> None:
    member = _find_invite(session, trip_id, user)
    if member is None:
        raise HTTPException(404, "No pending invite for this trip")
    member.user_id = user.id
    member.responded_at = dt.datetime.now(dt.timezone.utc)
    member.status = (
        MemberStatus.accepted if payload.action == "accept" else MemberStatus.declined
    )
    session.commit()


# --------------------------------------------------------------------------- #
# Waypoints (any trip member)
# --------------------------------------------------------------------------- #
@router.post("/{trip_id}/waypoints", response_model=WaypointOut, status_code=201)
def add_waypoint(
    trip_id: int, payload: WaypointIn, session: SessionDep, user: UserDep,
) -> WaypointOut:
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)
    wp = Waypoint(
        trip_id=trip.id,
        geom=from_shape(Point(payload.lng, payload.lat, payload.elevation_m), srid=4326),
        kind=payload.kind, label=payload.label, notes=payload.notes,
        created_by_id=user.id,
    )
    session.add(wp)
    session.commit()
    session.refresh(wp)
    return _waypoint_out(wp)


def _get_waypoint_or_404(session: Session, trip_id: int, wid: int) -> Waypoint:
    wp = session.get(Waypoint, wid)
    if wp is None or wp.trip_id != trip_id:
        raise HTTPException(404, "Waypoint not found")
    return wp


@router.patch("/{trip_id}/waypoints/{wid}", response_model=WaypointOut)
def update_waypoint(
    trip_id: int, wid: int, payload: WaypointUpdate, session: SessionDep, user: UserDep,
) -> WaypointOut:
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)
    wp = _get_waypoint_or_404(session, trip_id, wid)
    if payload.kind is not None:
        wp.kind = payload.kind
    if payload.label is not None:
        wp.label = payload.label
    if payload.notes is not None:
        wp.notes = payload.notes
    if payload.lng is not None and payload.lat is not None:
        z = payload.elevation_m if payload.elevation_m is not None else 0.0
        wp.geom = from_shape(Point(payload.lng, payload.lat, z), srid=4326)
    session.commit()
    session.refresh(wp)
    return _waypoint_out(wp)


@router.delete("/{trip_id}/waypoints/{wid}", status_code=204)
def delete_waypoint(
    trip_id: int, wid: int, session: SessionDep, user: UserDep,
) -> None:
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)
    wp = _get_waypoint_or_404(session, trip_id, wid)
    session.delete(wp)
    session.commit()


# --------------------------------------------------------------------------- #
# Trip routes (add/remove after creation)
# --------------------------------------------------------------------------- #
@router.post("/{trip_id}/routes", response_model=TripDetail, status_code=201)
def add_trip_route(
    trip_id: int, payload: TripRouteIn, session: SessionDep, user: UserDep,
) -> TripDetail:
    """Any accepted trip member may add one of their own routes to the trip."""
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)

    route = session.get(Route, payload.route_id)
    if route is None or route.owner_id != user.id:
        raise HTTPException(400, "Route not found or not yours")

    day = max(1, min(payload.day, trip.num_days))
    existing_count = len(session.scalars(
        select(TripRoute).where(TripRoute.trip_id == trip_id, TripRoute.day == day)
    ).all())

    session.add(TripRoute(trip_id=trip_id, route_id=payload.route_id, day=day, ordering=existing_count))
    session.commit()
    session.refresh(trip)
    return _to_detail(session, trip)


@router.delete("/{trip_id}/routes/{trip_route_id}", status_code=204)
def remove_trip_route(
    trip_id: int, trip_route_id: int, session: SessionDep, user: UserDep,
) -> None:
    """Trip owner can remove any route; members can only remove routes they own."""
    trip = _get_trip_or_404(session, trip_id)
    assert_can_view_trip(user, trip, session=session)

    tr = session.get(TripRoute, trip_route_id)
    if tr is None or tr.trip_id != trip_id:
        raise HTTPException(404, "Route assignment not found")

    if trip.owner_id != user.id:
        route = session.get(Route, tr.route_id)
        if route is None or route.owner_id != user.id:
            raise HTTPException(403, "Not the trip owner or route owner")

    session.delete(tr)
    session.commit()
