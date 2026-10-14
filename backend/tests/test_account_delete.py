"""DELETE /auth/me for a user who owns/participates in routes, trips, shares,
waypoints, friendships and invites. No FK cascades exist, so the endpoint must
clean up every referencing row itself.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from geoalchemy2.shape import from_shape
from shapely.geometry import LineString, Point
from sqlalchemy import or_, select

from app.auth.service import hash_password
from app.models import (
    Friendship, FriendStatus, MemberRole, MemberStatus, Route, RouteShare, Trip,
    TripMember, TripRoute, User, Waypoint,
)


def _route(owner: User, name: str) -> Route:
    return Route(
        owner_id=owner.id, name=name, region="colorado",
        geom=from_shape(LineString([(-106.4, 39.1, 3000), (-106.39, 39.11, 3100)]), srid=4326),
        summary={}, samples=[],
    )


def _trip(owner: User, name: str) -> Trip:
    return Trip(owner_id=owner.id, name=name, date=date(2026, 6, 1), region="colorado")


def _wp(trip: Trip, creator: User) -> Waypoint:
    return Waypoint(
        trip_id=trip.id, created_by_id=creator.id,
        geom=from_shape(Point(-106.4, 39.1, 3000), srid=4326),
    )


def _member(trip, user, role=MemberRole.member, status=MemberStatus.accepted,
            invited=None, responded=None):
    return TripMember(
        trip_id=trip.id, user_id=user.id, invited_email=user.email, role=role,
        status=status, invited_at=invited or datetime.now(timezone.utc),
        responded_at=responded,
    )


def test_delete_me_cleans_up_everything(db_session, users, make_client):
    owner, other, third = users["owner"], users["other"], users["third"]
    now = datetime.now(timezone.utc)

    route_a, route_b = _route(owner, "A"), _route(owner, "B")
    other_route = _route(other, "keep")
    db_session.add_all([route_a, route_b, other_route])
    db_session.flush()
    db_session.add(RouteShare(route_id=route_b.id, token="tok-b", created_by_id=owner.id))
    # a share on someone else's route created by the deleted user must go too
    db_session.add(RouteShare(route_id=other_route.id, token="tok-o", created_by_id=owner.id))

    # T1: owned by owner, with two accepted members -> passes to whoever joined
    # first by responded_at (other), even though third was invited earlier
    t1 = _trip(owner, "T1")
    # T2: owned by owner, only a pending invite -> deleted
    t2 = _trip(owner, "T2")
    # T3: owned by other, owner is a member
    t3 = _trip(other, "T3")
    # T4: owned by third, owner has an invite by email only (no user_id)
    t4 = _trip(third, "T4")
    db_session.add_all([t1, t2, t3, t4])
    db_session.flush()

    db_session.add_all([
        TripRoute(trip_id=t1.id, route_id=route_a.id),
        TripRoute(trip_id=t3.id, route_id=route_a.id),
        TripRoute(trip_id=t3.id, route_id=other_route.id),
        _member(t1, owner, MemberRole.owner, invited=now - timedelta(days=9)),
        _member(t1, third, invited=now - timedelta(days=8), responded=now - timedelta(days=1)),
        _member(t1, other, invited=now - timedelta(days=2), responded=now - timedelta(days=5)),
        _member(t2, owner, MemberRole.owner),
        TripMember(trip_id=t2.id, user_id=None, invited_email="nobody@example.com",
                   status=MemberStatus.invited, role=MemberRole.member),
        _member(t3, other, MemberRole.owner),
        _member(t3, owner),
        _member(t4, third, MemberRole.owner),
        TripMember(trip_id=t4.id, user_id=None, invited_email=owner.email.upper(),
                   status=MemberStatus.invited, role=MemberRole.member),
        _wp(t1, owner), _wp(t1, other), _wp(t2, owner), _wp(t3, owner), _wp(t3, other),
        Friendship(requester_id=owner.id, addressee_id=other.id, status=FriendStatus.accepted),
        Friendship(requester_id=third.id, addressee_id=owner.id, status=FriendStatus.pending),
        Friendship(requester_id=other.id, addressee_id=third.id, status=FriendStatus.accepted),
    ])
    db_session.flush()
    owner_id, owner_email = owner.id, owner.email
    ids = {k: v.id for k, v in dict(t1=t1, t2=t2, t3=t3, t4=t4, ra=route_a, rb=route_b,
                                    ro=other_route, other=other, third=third).items()}

    owner.hashed_password = hash_password("owner password")
    db_session.flush()
    r = make_client("owner").request("DELETE", "/auth/me", json={"password": "owner password"})
    assert r.status_code == 204

    s = db_session
    s.expire_all()
    assert s.get(User, owner_id) is None
    assert not s.scalars(select(Route).where(Route.owner_id == owner_id)).all()
    assert s.get(Route, ids["ro"]) is not None
    assert not s.scalars(select(TripRoute).where(TripRoute.route_id.in_([ids["ra"], ids["rb"]]))).all()
    assert s.scalars(select(TripRoute).where(TripRoute.trip_id == ids["t3"])).all()
    assert [x.token for x in s.scalars(select(RouteShare)).all() if x.token.startswith("tok-")] == []

    # T1 passed to the oldest accepted member
    t1_row = s.get(Trip, ids["t1"])
    assert t1_row.owner_id == ids["other"]
    roles = {m.user_id: m.role for m in s.scalars(select(TripMember).where(TripMember.trip_id == ids["t1"]))}
    assert roles == {ids["other"]: MemberRole.owner, ids["third"]: MemberRole.member}
    # waypoints on the transferred trip stay, reassigned to the new owner
    assert [w.created_by_id for w in s.scalars(select(Waypoint).where(Waypoint.trip_id == ids["t1"]))] == [ids["other"]] * 2

    # T2 had nobody else -> gone with its children
    assert s.get(Trip, ids["t2"]) is None
    assert not s.scalars(select(TripMember).where(TripMember.trip_id == ids["t2"])).all()
    assert not s.scalars(select(Waypoint).where(Waypoint.trip_id == ids["t2"])).all()

    # Memberships/invites removed, waypoints kept (reassigned), trips kept
    assert s.get(Trip, ids["t3"]).owner_id == ids["other"]
    assert not s.scalars(select(TripMember).where(
        or_(TripMember.user_id == owner_id, TripMember.invited_email.ilike(owner_email)))).all()
    assert [w.created_by_id for w in s.scalars(select(Waypoint).where(Waypoint.trip_id == ids["t3"]))] == [ids["other"]] * 2
    assert s.get(Trip, ids["t4"]) is not None

    # Friendships involving the user gone; unrelated ones kept
    fs = s.scalars(select(Friendship)).all()
    assert not [f for f in fs if owner_id in (f.requester_id, f.addressee_id)]
    assert any(f.requester_id == ids["other"] and f.addressee_id == ids["third"] for f in fs)
