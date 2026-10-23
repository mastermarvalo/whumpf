"""Deleting Strava data, as Strava's API Policy requires (docs/runbook.md §14).

- Disconnect, or the athlete revoking whumpf on Strava (deauthorization
  webhook, or the refresh job finding our tokens dead): delete the
  connection (tokens) and every route imported from Strava, copies included
  (§2.5, §7.4), then email a confirmation (§2.5).
- An activity deleted on Strava (webhook, or the refresh job getting a 404):
  delete the routes imported from it (§6.2, §6.3).

Strava's webhook events aren't signed and athlete ids are public, so an event
alone deletes nothing: ``handle_event`` asks Strava first, and only deletes
when Strava confirms the token is revoked or the activity is gone.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.route import Route
from app.models.route_share import RouteShare
from app.models.strava import StravaConnection
from app.models.trip_route import TripRoute
from app.models.user import User
from app.services import email
from app.services import strava as strava_svc

logger = logging.getLogger("whumpf.strava")

WHY_DISCONNECTED = "You disconnected Strava from whumpf."
WHY_REVOKED = "You removed whumpf's access in your Strava settings."


def delete_routes(session: Session, route_ids: list[int]) -> None:
    """Delete routes with their trip-day links and share links (no FK
    cascades: children first, flushed before the parents)."""
    if not route_ids:
        return
    session.execute(delete(TripRoute).where(TripRoute.route_id.in_(route_ids)))
    session.execute(delete(RouteShare).where(RouteShare.route_id.in_(route_ids)))
    session.flush()
    session.execute(delete(Route).where(Route.id.in_(route_ids)))
    session.flush()


def purge_user(session: Session, user_id: int) -> int:
    """Delete the user's Strava connection and every route imported from
    Strava (copies keep ``source_strava_id``, so they go too). Routes they drew
    stay. Returns the number of routes deleted; the caller commits."""
    ids = list(session.scalars(
        select(Route.id).where(Route.owner_id == user_id, Route.source_strava_id.is_not(None))
    ))
    delete_routes(session, ids)
    session.execute(delete(StravaConnection).where(StravaConnection.user_id == user_id))
    session.flush()
    return len(ids)


def delete_activity_routes(session: Session, user_id: int, activity_id: int) -> int:
    """Delete the user's routes imported from one activity (and copies of
    them). Returns how many; the caller commits."""
    ids = list(session.scalars(
        select(Route.id).where(Route.owner_id == user_id, Route.source_strava_id == activity_id)
    ))
    delete_routes(session, ids)
    return len(ids)


async def confirm_deletion(to: str, why: str, routes_deleted: int) -> None:
    """Best-effort confirmation email; a mail failure never undoes a deletion."""
    try:
        await email.send_strava_data_deleted(to=to, why=why, routes_deleted=routes_deleted)
    except Exception:
        logger.warning("Strava deletion confirmation email failed", exc_info=True)


async def purge_revoked(session: Session, user_id: int) -> int:
    """Strava confirmed the athlete revoked whumpf: purge, commit, confirm."""
    n = purge_user(session, user_id)
    session.commit()
    logger.info("Strava access revoked: deleted Strava data for user_id=%s (%d routes)", user_id, n)
    user = session.get(User, user_id)
    if user is not None:
        await confirm_deletion(user.email, WHY_REVOKED, n)
    return n


# ── webhook events ────────────────────────────────────────────────────────────

def is_deauthorization(event: dict[str, Any]) -> bool:
    updates = event.get("updates") or {}
    return (
        event.get("object_type") == "athlete"
        and isinstance(updates, dict)
        and str(updates.get("authorized", "")).lower() == "false"
    )


def is_activity_delete(event: dict[str, Any]) -> bool:
    return event.get("object_type") == "activity" and event.get("aspect_type") == "delete"


async def handle_event(session: Session, event: dict[str, Any]) -> str:
    """Act on one Strava webhook event. Returns what happened (logged, and
    checked by tests): ignored, unknown_athlete, purged, kept_valid,
    kept_unknown, no_routes, routes_deleted."""
    athlete_id = event.get("owner_id")
    if not isinstance(athlete_id, int):
        return "ignored"
    deauth, activity_delete = is_deauthorization(event), is_activity_delete(event)
    if not (deauth or activity_delete):
        return "ignored"
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.strava_athlete_id == athlete_id)
    ).first()
    if conn is None:
        return "unknown_athlete"
    user_id = conn.user_id

    if deauth:
        state = await strava_svc.check_access(conn, session)
        if state == strava_svc.REVOKED:
            await purge_revoked(session, user_id)
            return "purged"
        if state == strava_svc.VALID:
            # Our tokens still work, so the athlete didn't revoke us: not from
            # Strava, or a stale retry after they reconnected.
            logger.warning("Strava deauthorization event for user_id=%s, but access still works; kept", user_id)
            return "kept_valid"
        logger.warning("Strava deauthorization event for user_id=%s: Strava couldn't confirm (%s); kept",
                       user_id, state)
        return "kept_unknown"

    activity_id = event.get("object_id")
    if not isinstance(activity_id, int):
        return "ignored"
    has_routes = session.scalars(
        select(Route.id).where(Route.owner_id == user_id, Route.source_strava_id == activity_id)
    ).first() is not None
    if not has_routes:
        return "no_routes"
    token, state = await strava_svc.access_token_or_state(conn, session)
    if token is not None:
        state, _ = await strava_svc.activity_state(token, activity_id)
    if state == strava_svc.GONE:
        n = delete_activity_routes(session, user_id, activity_id)
        session.commit()
        logger.info("Strava activity deleted: removed %d routes of user_id=%s", n, user_id)
        return "routes_deleted"
    if state == strava_svc.REVOKED:
        await purge_revoked(session, user_id)
        return "purged"
    if state == strava_svc.VALID:
        logger.warning("Strava activity delete event for user_id=%s, but the activity is still there; kept",
                       user_id)
        return "kept_valid"
    logger.warning("Strava activity delete event for user_id=%s: Strava couldn't confirm (%s); kept",
                   user_id, state)
    return "kept_unknown"
