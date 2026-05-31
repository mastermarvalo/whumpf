"""Reusable authorization checks for owner-scoped planning objects.

Authorization is enforced at the query/router layer, never only in the UI.
Use these helpers instead of repeating ownership checks in each endpoint.

Access to a route is **owner-or-valid-token only**. The route's `visibility`
value is a cosmetic label (set when a share link exists) and is deliberately
NOT consulted here — party-membership access is Phase C.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.friendship import Friendship, FriendStatus
from app.models.route import Route
from app.models.route_share import RouteShare
from app.models.trip import Trip
from app.models.trip_member import MemberStatus, TripMember
from app.models.user import User


def can_view_route(
    user: User,
    route: Route,
    *,
    session: Session | None = None,
    token: str | None = None,
) -> bool:
    """True if ``user`` may view ``route``.

    Owner can always view. Otherwise, a non-revoked share token *scoped to this
    route* grants view (and clone). Token validation needs a DB session, so both
    ``session`` and ``token`` must be supplied for the token path.
    """
    if route.owner_id == user.id:
        return True
    if token and session is not None:
        share = session.scalars(
            select(RouteShare).where(
                RouteShare.token == token,
                RouteShare.route_id == route.id,  # token must match THIS route
                RouteShare.revoked_at.is_(None),
            )
        ).first()
        if share is not None:
            return True
    return False


def can_edit_route(user: User, route: Route) -> bool:
    """True if ``user`` may modify or delete ``route``. Owner only — always."""
    return route.owner_id == user.id


def assert_can_view(
    user: User,
    route: Route,
    *,
    session: Session | None = None,
    token: str | None = None,
) -> None:
    """Raise 404 if the user cannot view the route.

    404 (not 403) so we don't leak the existence of routes the caller has no
    relationship to.
    """
    if not can_view_route(user, route, session=session, token=token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Route not found")


def assert_can_edit(user: User, route: Route) -> None:
    """Raise 403 if the user cannot edit the route."""
    if not can_edit_route(user, route):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not the route owner")


# --------------------------------------------------------------------------- #
# Trips & friends (Phase C)
# --------------------------------------------------------------------------- #
def is_trip_member(user: User, trip: Trip, *, session: Session) -> bool:
    """True if ``user`` owns the trip or is an accepted party member."""
    if trip.owner_id == user.id:
        return True
    member = session.scalars(
        select(TripMember).where(
            TripMember.trip_id == trip.id,
            TripMember.user_id == user.id,
            TripMember.status == MemberStatus.accepted,
        )
    ).first()
    return member is not None


def can_view_trip(user: User, trip: Trip, *, session: Session) -> bool:
    return is_trip_member(user, trip, session=session)


def assert_can_view_trip(user: User, trip: Trip, *, session: Session) -> None:
    """Raise 404 if the user is not a member (don't leak trip existence)."""
    if not can_view_trip(user, trip, session=session):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trip not found")


def assert_trip_owner(user: User, trip: Trip) -> None:
    """Raise 403 if the user is not the trip owner."""
    if trip.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not the trip owner")


def are_friends(a_id: int, b_id: int, *, session: Session) -> bool:
    """True if an accepted friendship exists between the two users (either dir)."""
    row = session.scalars(
        select(Friendship).where(
            Friendship.status == FriendStatus.accepted,
            or_(
                and_(Friendship.requester_id == a_id, Friendship.addressee_id == b_id),
                and_(Friendship.requester_id == b_id, Friendship.addressee_id == a_id),
            ),
        )
    ).first()
    return row is not None
