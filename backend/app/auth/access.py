"""Reusable authorization checks for owner-scoped planning objects.

Authorization is enforced at the query/router layer, never only in the UI.
Use these helpers instead of repeating ownership checks in each endpoint.

Access to a route is **owner-or-valid-token only**. The route's `visibility`
value is a cosmetic label (set when a share link exists) and is deliberately
NOT consulted here — party-membership access is Phase C.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.route import Route
from app.models.route_share import RouteShare
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
