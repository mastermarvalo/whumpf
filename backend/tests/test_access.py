"""Unit tests for the route access-control helpers (the highest-risk code)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth.access import (
    assert_can_edit,
    assert_can_view,
    can_edit_route,
    can_view_route,
)
from app.models.route import Route, Visibility


class _U:
    def __init__(self, uid: int) -> None:
        self.id = uid


def _route(owner_id: int, visibility: Visibility = Visibility.private) -> Route:
    r = Route()
    r.owner_id = owner_id
    r.visibility = visibility
    return r


def test_owner_can_view_and_edit() -> None:
    owner = _U(1)
    route = _route(owner_id=1)
    assert can_view_route(owner, route) is True
    assert can_edit_route(owner, route) is True
    assert_can_view(owner, route)  # no raise
    assert_can_edit(owner, route)  # no raise


def test_non_owner_cannot_view_or_edit_private() -> None:
    other = _U(2)
    route = _route(owner_id=1)
    assert can_view_route(other, route) is False
    assert can_edit_route(other, route) is False


def test_non_owner_view_raises_404() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_can_view(_U(2), _route(owner_id=1))
    assert exc.value.status_code == 404


def test_non_owner_edit_raises_403() -> None:
    with pytest.raises(HTTPException) as exc:
        assert_can_edit(_U(2), _route(owner_id=1))
    assert exc.value.status_code == 403


@pytest.mark.parametrize("vis", [Visibility.shared, Visibility.unlisted])
def test_non_owner_still_blocked_until_phase_b(vis: Visibility) -> None:
    # Phase A is owner-only regardless of visibility / token.
    other = _U(2)
    route = _route(owner_id=1, visibility=vis)
    assert can_view_route(other, route, token="anything") is False
