"""Auth endpoints: register, token (login), verify, reset, delete, current-user."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.admin import is_admin as _check_admin
from app.auth.dependencies import get_current_user
from app.auth.service import (
    clear_auth_cookie,
    create_access_token,
    hash_password,
    set_auth_cookie,
    verify_password,
)
from app.config import get_settings
from app.db import get_session
from app.models.strava import StravaConnection
from app.models.user import User
from app.rate_limit import limiter
from app.services import strava as strava_svc
from app.services.email import (
    send_password_reset_email,
    send_verification_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("whumpf.auth")


def _new_token() -> str:
    """Cryptographically secure URL-safe token (~43 chars)."""
    return secrets.token_urlsafe(32)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RegisterIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class RegisterOut(BaseModel):
    # When email verification is required, registration does NOT start a session —
    # the user must verify first. `access_token` is only set (and a cookie issued)
    # when the account is immediately usable (skip-verification mode).
    email_verified: bool
    access_token: str | None = None
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str
    email_verified: bool
    is_admin: bool


class EmailIn(BaseModel):
    email: str


class TokenIn(BaseModel):
    token: str


class PasswordResetConfirmIn(BaseModel):
    token: str
    new_password: str


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
async def register(
    request: Request,
    response: Response,
    body: RegisterIn,
    session: Session = Depends(get_session),
) -> RegisterOut:
    email = body.email.lower().strip()
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid email address")
    if len(body.password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
    if session.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

    settings = get_settings()
    user = User(email=email, hashed_password=hash_password(body.password))
    if settings.skip_email_verification:
        user.email_verified = True
        user.email_verified_at = _now()
    else:
        token = _new_token()
        user.email_verification_token = token
        user.email_verification_token_expires_at = _now() + timedelta(
            seconds=settings.email_verification_ttl_s,
        )
    session.add(user)
    session.commit()
    logger.info("New user registered: id=%s email_verified=%s", user.id, user.email_verified)

    if not settings.skip_email_verification:
        try:
            await send_verification_email(to=user.email, token=token)
        except Exception as exc:
            logger.warning("Verification email send failed for user %s: %s", user.id, exc)
        # Verification required → do NOT start a session. The user must verify
        # their email, then sign in.
        return RegisterOut(email_verified=False)

    access = create_access_token(user.email)
    set_auth_cookie(response, access)
    return RegisterOut(email_verified=True, access_token=access)


@router.post("/token", response_model=TokenOut)
@limiter.limit("10/minute")
def login(
    request: Request,
    response: Response,
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> TokenOut:
    email = form.username.lower().strip()
    user = session.scalars(select(User).where(User.email == email)).first()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not get_settings().skip_email_verification and not user.email_verified:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Please verify your email before signing in — check your inbox for the link.",
        )
    token = create_access_token(user.email)
    set_auth_cookie(response, token)
    return TokenOut(access_token=token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> Response:
    """Clear the session cookie. Idempotent — safe to call without a session."""
    clear_auth_cookie(response)
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    # is_admin combines the per-row column + the ADMIN_EMAILS env allowlist,
    # so the frontend gets a single boolean to gate admin UI on.
    return UserOut(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        is_admin=_check_admin(user),
    )


# ── email verification ─────────────────────────────────────────────────────────

@router.post("/verify-email/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def verify_email_request(
    request: Request,
    body: EmailIn,
    session: Session = Depends(get_session),
) -> Response:
    """Resend a verification link. Always returns 204 to avoid revealing
    whether an account exists. Already-verified accounts are silently
    skipped — same response — so attackers can't probe verification state.
    """
    email = body.email.lower().strip()
    user = session.scalars(select(User).where(User.email == email)).first()
    if user and not user.email_verified:
        settings = get_settings()
        token = _new_token()
        user.email_verification_token = token
        user.email_verification_token_expires_at = _now() + timedelta(
            seconds=settings.email_verification_ttl_s,
        )
        session.commit()
        try:
            await send_verification_email(to=user.email, token=token)
        except Exception as exc:
            logger.warning("Verification email send failed for user %s: %s", user.id, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
def verify_email_confirm(
    request: Request,
    body: TokenIn,
    session: Session = Depends(get_session),
) -> Response:
    user = session.scalars(
        select(User).where(User.email_verification_token == body.token),
    ).first()
    if not user or not user.email_verification_token_expires_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    if user.email_verification_token_expires_at < _now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token expired")
    user.email_verified = True
    user.email_verified_at = _now()
    user.email_verification_token = None
    user.email_verification_token_expires_at = None
    session.commit()
    logger.info("Email verified: user_id=%s", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── password reset ─────────────────────────────────────────────────────────────

@router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def password_reset_request(
    request: Request,
    body: EmailIn,
    session: Session = Depends(get_session),
) -> Response:
    """Always returns 204, regardless of whether the email exists, to avoid
    email enumeration. Reset links expire after `password_reset_ttl_s` seconds.
    """
    email = body.email.lower().strip()
    user = session.scalars(select(User).where(User.email == email)).first()
    if user and user.is_active:
        settings = get_settings()
        token = _new_token()
        user.password_reset_token = token
        user.password_reset_token_expires_at = _now() + timedelta(
            seconds=settings.password_reset_ttl_s,
        )
        session.commit()
        try:
            await send_password_reset_email(to=user.email, token=token)
        except Exception as exc:
            logger.warning("Password-reset email send failed for user %s: %s", user.id, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmIn,
    session: Session = Depends(get_session),
) -> Response:
    if len(body.new_password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")
    user = session.scalars(
        select(User).where(User.password_reset_token == body.token),
    ).first()
    if not user or not user.password_reset_token_expires_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    if user.password_reset_token_expires_at < _now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token expired")
    user.hashed_password = hash_password(body.new_password)
    user.password_reset_token = None
    user.password_reset_token_expires_at = None
    session.commit()
    logger.info("Password reset: user_id=%s", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── account deletion ───────────────────────────────────────────────────────────

@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(
    response: Response,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Self-serve account deletion. Best-effort server-side Strava revoke
    happens first; if it fails, the local user row is still deleted so the
    user isn't trapped by a dead upstream.
    """
    conn = session.scalars(
        select(StravaConnection).where(StravaConnection.user_id == user.id),
    ).first()
    if conn:
        try:
            access_token = await strava_svc.refresh_token(conn, session)
            await strava_svc.deauthorize(access_token)
        except Exception as exc:
            logger.warning("Strava deauthorize failed during account delete for user %s: %s",
                           user.id, exc)
        session.delete(conn)

    logger.info("Account deleted: user_id=%s", user.id)
    session.delete(user)
    session.commit()
    clear_auth_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
