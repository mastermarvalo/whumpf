"""Reusable authorization checks for owner-scoped planning objects.

Authorization is enforced at the query/router layer, never only in the UI.
Use these helpers instead of repeating ownership checks in each endpoint.

Phase A is owner-only. The share-token / party-membership branches are added
in later phases — the extension points are marked below.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from app.models.route import Route, Visibility
from app.models.user import User


def can_view_route(user: User, route: Route, *, token: str | None = None) -> bool:
    """True if ``user`` may view ``route``.

    Owner can always view. (Phase B: ``shared`` routes are viewable by party
    members; ``unlisted`` routes are viewable by anyone presenting a valid,
    non-revoked share token — that branch lands with the route_shares model.)
    """
    if route.owner_id == user.id:
        return True
    # Phase B: party membership and valid `token` against route_shares.
    _ = (token, Visibility)  # referenced now so the extension point is explicit
    return False


def can_edit_route(user: User, route: Route) -> bool:
    """True if ``user`` may modify or delete ``route``. Owner only — always."""
    return route.owner_id == user.id


def assert_can_view(user: User, route: Route, *, token: str | None = None) -> None:
    """Raise 404 if the user cannot view the route.

    404 (not 403) so we don't leak the existence of routes the caller has no
    relationship to.
    """
    if not can_view_route(user, route, token=token):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Route not found")


def assert_can_edit(user: User, route: Route) -> None:
    """Raise 403 if the user cannot edit the route."""
    if not can_edit_route(user, route):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not the route owner")
