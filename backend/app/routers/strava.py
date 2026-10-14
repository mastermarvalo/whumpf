"""Strava OAuth and data endpoints."""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse  # used by callback
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
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


_STATE_PURPOSE = "strava_oauth"
_STATE_TTL = timedelta(minutes=10)
_RETURN_TO_MAX_LEN = 2048


def validate_return_to(url: str) -> bool:
    """Allowlist for where the callback may send the user afterwards.

    Always: whumpf://... and https://whumpf.co/... . With
    STRAVA_RETURN_TO_ALLOW_DEV also exp://... and http://localhost:8081/... .
    Hosts must match exactly (no userinfo, other ports or subdomains).
    """
    if len(url) > _RETURN_TO_MAX_LEN:
        return False
    if any(c.isspace() or c == "\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if url[:9].lower() == "whumpf://":
        return True
    if parts.scheme == "https" and parts.netloc == "whumpf.co":
        return True
    if get_settings().strava_return_to_allow_dev:
        if url[:6].lower() == "exp://":
            return True
        if parts.scheme == "http" and parts.netloc == "localhost:8081":
            return True
    return False


def _make_state(user_id: int, return_to: str | None) -> str:
    """Signed OAuth state. Has no ``sub``, so it is never accepted as a login
    token (and login tokens are never accepted as state)."""
    s = get_settings()
    payload = {
        "purpose": _STATE_PURPOSE,
        "uid": user_id,
        "rt": return_to,
        "nonce": secrets.token_urlsafe(8),
        "exp": datetime.now(timezone.utc) + _STATE_TTL,
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


@dataclass
class _OAuthState:
    user_id: int
    return_to: str | None
    expired: bool


def _read_state(token: str) -> _OAuthState | None:
    """Verify the state's signature and purpose. Expiry is reported rather than
    rejected: a validly signed state's return_to is still trustworthy, so the
    caller can send the user back with reason=expired. None = forged/tampered."""
    s = get_settings()
    try:
        payload = jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"verify_exp": False}
        )
    except JWTError:
        return None
    uid, exp, rt = payload.get("uid"), payload.get("exp"), payload.get("rt")
    if payload.get("purpose") != _STATE_PURPOSE or not isinstance(uid, int):
        return None
    if not isinstance(exp, (int, float)):
        return None
    # Re-check against the current allowlist in case it was tightened since.
    if rt is not None and not (isinstance(rt, str) and validate_return_to(rt)):
        return None
    return _OAuthState(user_id=uid, return_to=rt, expired=exp <= time.time())


def _with_params(url: str, **params: str) -> str:
    """Set query params on ``url``, keeping its other params and fragment."""
    parts = urlsplit(url)
    # Drop any stale strava/reason so the outcome is unambiguous.
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in ("strava", "reason")
    ]
    query.extend(params.items())
    return urlunsplit(parts._replace(query=urlencode(query)))


@router.get("/auth/strava/authorize", response_model=AuthorizeOut)
def strava_authorize(
    return_to: str | None = None,
    user: User = Depends(get_current_user),
) -> AuthorizeOut:
    """Return the Strava OAuth consent URL. The client navigates there directly.

    ``return_to`` (optional, allowlisted) is where the callback redirects
    afterwards; without it the callback uses STRAVA_SUCCESS_URL.
    """
    if return_to is not None and not validate_return_to(return_to):
        raise HTTPException(400, "return_to is not allowed")
    state = _make_state(user.id, return_to)
    return AuthorizeOut(url=strava_svc.get_authorize_url(state))


@router.get("/auth/strava/callback")
async def strava_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Strava redirects here after the user approves (or denies) the app.

    With a return_to in the state: redirect there with ?strava=connected or
    ?strava=error&reason=denied|expired|bad_state|token_exchange_failed.
    Without one: the legacy STRAVA_SUCCESS_URL redirects / 400s.
    """
    settings = get_settings()
    success_url = settings.strava_success_url
    oauth_state = _read_state(state) if state else None
    return_to = oauth_state.return_to if oauth_state else None

    def fail(reason: str, legacy: str | None) -> RedirectResponse:
        if return_to:
            url = _with_params(return_to, strava="error", reason=reason)
            return RedirectResponse(url, status_code=302)
        if legacy is None:
            raise HTTPException(400, "Invalid OAuth state")
        return RedirectResponse(f"{success_url}?strava={legacy}", status_code=302)

    if error or not code or not state:
        logger.warning("Strava OAuth denied or missing params: error=%s", error)
        return fail("denied", "denied")

    if oauth_state is None:
        logger.warning("Strava OAuth state invalid")
        raise HTTPException(400, "Invalid OAuth state")
    if oauth_state.expired:
        logger.warning("Strava OAuth state expired: user_id=%s", oauth_state.user_id)
        return fail("expired", None)

    user = session.get(User, oauth_state.user_id)
    if not user:
        logger.warning("Strava OAuth state for missing user_id=%s", oauth_state.user_id)
        return fail("bad_state", None)

    try:
        token_data = await strava_svc.exchange_code(code)
    except Exception as exc:
        logger.error("Strava token exchange failed: %s", exc)
        return fail("token_exchange_failed", "error")

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

    logger.info("Strava connected: user_id=%s athlete_id=%s", user.id, strava_id)
    if return_to:
        return RedirectResponse(_with_params(return_to, strava="connected"), status_code=302)
    return RedirectResponse(f"{success_url}?strava=connected", status_code=302)


@router.delete("/auth/strava/disconnect", status_code=204)
async def strava_disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> None:
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id)
    ).first()
    if not conn:
        return
    # Best-effort server-side revoke. If Strava is down or the token is dead,
    # local row deletion is the source of truth — the user shouldn't be unable
    # to disconnect just because an upstream is unreachable.
    try:
        access_token = await strava_svc.refresh_token(conn, session)
        await strava_svc.deauthorize(access_token)
    except Exception as exc:
        logger.warning("Strava deauthorize failed for user %s: %s", user.id, exc)
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
