"""Strava OAuth and data endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse  # used by callback
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.service import create_access_token, decode_token
from app.db import get_session
from app.models.strava import StravaConnection
from app.models.user import User
from app.services import strava as strava_svc
from app.config import get_settings

router = APIRouter(tags=["strava"])
logger = logging.getLogger("whumpf.strava")


class StravaStatusOut(BaseModel):
    connected: bool
    athlete_name: str | None = None
    athlete_icon_url: str | None = None


# ── OAuth ──────────────────────────────────────────────────────────────────────

class AuthorizeOut(BaseModel):
    url: str


@router.get("/auth/strava/authorize", response_model=AuthorizeOut)
def strava_authorize(user: User = Depends(get_current_user)) -> AuthorizeOut:
    """Return the Strava OAuth consent URL. The client navigates there directly."""
    state = create_access_token(user.email)
    return AuthorizeOut(url=strava_svc.get_authorize_url(state))


@router.get("/auth/strava/callback")
async def strava_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Strava redirects here after the user approves (or denies) the app."""
    settings = get_settings()
    success_url = settings.strava_success_url

    if error or not code or not state:
        logger.warning("Strava OAuth denied or missing params: error=%s", error)
        return RedirectResponse(f"{success_url}?strava=denied", status_code=302)

    email = decode_token(state)
    if not email:
        raise HTTPException(400, "Invalid OAuth state")

    user = session.scalars(select(User).where(User.email == email)).first()
    if not user:
        raise HTTPException(400, "User not found for OAuth state")

    try:
        token_data = await strava_svc.exchange_code(code)
    except Exception as exc:
        logger.error("Strava token exchange failed: %s", exc)
        return RedirectResponse(f"{success_url}?strava=error", status_code=302)

    athlete = token_data.get("athlete", {})
    strava_id = athlete.get("id")
    athlete_name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    icon_url = athlete.get("profile_medium") or athlete.get("profile")

    # Upsert the connection.
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if conn is None:
        conn = StravaConnection(user_id=user.id)
        session.add(conn)

    conn.strava_athlete_id = strava_id
    conn.access_token = token_data["access_token"]
    conn.refresh_token = token_data["refresh_token"]
    conn.expires_at = token_data["expires_at"]
    conn.scope = token_data.get("scope", "")
    conn.athlete_name = athlete_name
    conn.athlete_icon_url = icon_url
    session.commit()

    logger.info("Strava connected: user=%s athlete=%s (%d)", email, athlete_name, strava_id)
    return RedirectResponse(f"{success_url}?strava=connected", status_code=302)


@router.delete("/auth/strava/disconnect", status_code=204)
def strava_disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if conn:
        session.delete(conn)
        session.commit()


# ── Data ───────────────────────────────────────────────────────────────────────

@router.get("/strava/status", response_model=StravaStatusOut)
def strava_status(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StravaStatusOut:
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if not conn:
        return StravaStatusOut(connected=False)
    return StravaStatusOut(
        connected=True,
        athlete_name=conn.athlete_name or None,
        athlete_icon_url=conn.athlete_icon_url,
    )


class ActivityDetailOut(BaseModel):
    description: str | None = None
    photo_url: str | None = None


@router.get("/strava/activities/{activity_id}", response_model=ActivityDetailOut)
async def strava_activity_detail(
    activity_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ActivityDetailOut:
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if not conn:
        raise HTTPException(404, "Strava not connected")
    try:
        access_token = await strava_svc.refresh_token(conn, session)
        detail = await strava_svc.fetch_activity_detail(access_token, activity_id)
    except Exception as exc:
        logger.error("Strava activity detail fetch failed: %s", exc)
        raise HTTPException(502, f"Strava unavailable: {exc}") from exc
    primary = (detail.get("photos") or {}).get("primary") or {}
    photo_urls = primary.get("urls") or {}
    photo_url = photo_urls.get("600") or photo_urls.get("100") or None
    return ActivityDetailOut(
        description=detail.get("description") or None,
        photo_url=photo_url,
    )


@router.get("/strava/activities")
async def strava_activities(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Return a GeoJSON FeatureCollection of the user's recent Strava activities."""
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if not conn:
        raise HTTPException(404, "Strava not connected")

    try:
        access_token = await strava_svc.refresh_token(conn, session)
        activities = await strava_svc.fetch_activities(access_token)
    except Exception as exc:
        logger.error("Strava activities fetch failed: %s", exc)
        raise HTTPException(502, f"Strava unavailable: {exc}") from exc

    return strava_svc.activities_to_geojson(activities)
