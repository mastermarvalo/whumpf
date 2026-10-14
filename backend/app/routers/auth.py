"""Auth endpoints: register, token (login), verify, reset, change password /
email, delete, current-user."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, model_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.admin import is_admin as _check_admin
from app.auth.codes import (
    CodeError,
    check_code,
    discard_codes,
    hash_link_token,
    issue_code,
    new_link_token,
)
from app.auth.dependencies import get_current_user
from app.auth.service import (
    RefreshTokenError,
    clear_auth_cookie,
    create_access_token,
    hash_password,
    issue_refresh_token,
    live_refresh_family,
    revoke_all_refresh_tokens,
    revoke_refresh_family,
    rotate_refresh_token,
    set_auth_cookie,
    verify_password,
)
from app.config import get_settings
from app.db import get_session
from app.models import (
    CodePurpose, EmailCode, Friendship, MemberRole, MemberStatus, RefreshToken, Route,
    RouteShare, Trip, TripMember, TripRoute, Waypoint,
)
from app.models.strava import StravaConnection
from app.models.user import User
from app.rate_limit import enforce_key_limits, limiter
from app.services import strava as strava_svc
from app.services.email import (
    send_email_change_code,
    send_password_reset_email,
    send_security_notice,
    send_verification_email,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("whumpf.auth")

# Per-address limits on sending a code, on top of the per-IP decorators.
_SEND_LIMITS = ("3/15minutes", "10/day")
# Per-address limit on code guesses, across codes (each code also allows only
# MAX_ATTEMPTS wrong guesses).
_CHECK_LIMITS = ("10/hour",)

_BAD_CODE = "Invalid or expired code"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_email(raw: str) -> str:
    return raw.lower().strip()


class RegisterIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    # Body-only, for app clients: swap it at POST /auth/refresh when the access
    # token expires. Browsers keep using the session cookie.
    refresh_token: str
    token_type: str = "bearer"


class RefreshIn(BaseModel):
    refresh_token: str


class LogoutIn(BaseModel):
    refresh_token: str | None = None


class RegisterOut(BaseModel):
    # When email verification is required, registration does NOT start a session —
    # the user must verify first. `access_token` is only set (and a cookie issued)
    # when the account is immediately usable (skip-verification mode).
    email_verified: bool
    access_token: str | None = None
    refresh_token: str | None = None
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str
    email_verified: bool
    is_admin: bool


class EmailIn(BaseModel):
    email: str


class VerifyEmailConfirmIn(BaseModel):
    # Either email + code (app), or token (legacy link from the old website).
    email: str | None = None
    code: str | None = None
    token: str | None = None

    @model_validator(mode="after")
    def _code_or_token(self) -> "VerifyEmailConfirmIn":
        if not (self.token or (self.email and self.code)):
            raise ValueError("Send email and code")
        return self


class PasswordResetConfirmIn(VerifyEmailConfirmIn):
    new_password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str
    # App clients send their refresh token so their own session survives;
    # every other refresh token is revoked.
    refresh_token: str | None = None


class ChangeEmailIn(BaseModel):
    new_email: str
    current_password: str


class CodeIn(BaseModel):
    code: str


class DeleteMeIn(BaseModel):
    password: str


class ChangeEmailOut(BaseModel):
    # The access token is keyed on email, so the old one stops working.
    email: str
    access_token: str
    token_type: str = "bearer"


@router.post("/register", response_model=RegisterOut, status_code=status.HTTP_201_CREATED)
@limiter.limit("30/hour")
async def register(
    request: Request,
    response: Response,
    body: RegisterIn,
    session: Session = Depends(get_session),
) -> RegisterOut:
    email = _norm_email(body.email)
    if "@" not in email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid email address")
    _check_new_password(body.password)
    if session.scalars(select(User).where(User.email == email)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")

    settings = get_settings()
    user = User(email=email, hashed_password=hash_password(body.password))
    if settings.skip_email_verification:
        user.email_verified = True
        user.email_verified_at = _now()
    session.add(user)
    session.commit()
    logger.info("New user registered: id=%s email_verified=%s", user.id, user.email_verified)

    if not settings.skip_email_verification:
        await _send_verification(session, user)
        # Verification required → do NOT start a session. The user must verify
        # their email, then sign in.
        return RegisterOut(email_verified=False)

    access = create_access_token(user.email)
    refresh_token = issue_refresh_token(session, user.id)
    session.commit()
    set_auth_cookie(response, access)
    return RegisterOut(email_verified=True, access_token=access, refresh_token=refresh_token)


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
            "Please verify your email before signing in — check your inbox.",
        )
    token = create_access_token(user.email)
    refresh_token = issue_refresh_token(session, user.id)
    session.commit()
    set_auth_cookie(response, token)
    return TokenOut(access_token=token, refresh_token=refresh_token)


@router.post("/refresh", response_model=TokenOut)
@limiter.limit("30/minute")
def refresh(
    request: Request,
    body: RefreshIn,
    session: Session = Depends(get_session),
) -> TokenOut:
    """Swap a refresh token for a new access + refresh token pair. The old
    refresh token is revoked; see rotate_refresh_token for reuse handling.
    Sets no cookie — the cookie flow doesn't use refresh tokens."""
    try:
        user, new_refresh = rotate_refresh_token(session, body.refresh_token)
    except RefreshTokenError as exc:
        session.commit()  # keep a reuse revocation
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, str(exc), headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    session.commit()
    return TokenOut(access_token=create_access_token(user.email), refresh_token=new_refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    body: LogoutIn | None = None,
    session: Session = Depends(get_session),
) -> None:
    """Clear the session cookie and, if a refresh token is sent, revoke it
    (and the rest of its family). Idempotent — safe to call without a session."""
    if body and body.refresh_token:
        revoke_refresh_family(session, body.refresh_token)
        session.commit()
    clear_auth_cookie(response)


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


# ── shared helpers ─────────────────────────────────────────────────────────────

def _check_new_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password must be at least 8 characters")


def _check_current_password(user: User, password: str) -> None:
    if not verify_password(password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")


def _user_from_code(session: Session, email: str, purpose: CodePurpose, code: str) -> User:
    """User whose live ``purpose`` code for ``email`` is ``code``; consumes it.
    Unknown email and wrong code look the same."""
    enforce_key_limits(f"{purpose.value}-check", email, *_CHECK_LIMITS)
    user = session.scalars(select(User).where(User.email == email)).first()
    if not user:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    try:
        check_code(session, user.id, purpose, code)
    except CodeError as exc:
        session.commit()  # keep the used-up attempt
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE) from exc
    return user


# Legacy link flow (old website). Remove with LEGACY_EMAIL_LINKS, along with
# the users.*_token columns.
def _user_from_link(
    session: Session,
    token_col: InstrumentedAttribute,
    expires_col: InstrumentedAttribute,
    token: str,
) -> User:
    if not get_settings().legacy_email_links:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    user = session.scalars(select(User).where(token_col == hash_link_token(token))).first()
    expires_at = getattr(user, expires_col.key) if user else None
    if not expires_at:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired token")
    if expires_at < _now():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Token expired")
    return user


async def _notify(user_id: int, to: str, what: str) -> None:
    try:
        await send_security_notice(to=to, what=what)
    except Exception as exc:
        logger.warning("Security notice send failed for user %s: %s", user_id, exc)


# ── email verification ─────────────────────────────────────────────────────────

async def _send_verification(session: Session, user: User) -> None:
    """Issue a code (plus a legacy link token) and email it. Commits."""
    settings = get_settings()
    code = issue_code(session, user.id, CodePurpose.verify_email, user.email)
    token = None
    if settings.legacy_email_links:
        token, user.email_verification_token = new_link_token()
        user.email_verification_token_expires_at = _now() + timedelta(
            seconds=settings.email_verification_ttl_s,
        )
    session.commit()
    try:
        await send_verification_email(to=user.email, code=code, token=token)
    except Exception as exc:
        logger.warning("Verification email send failed for user %s: %s", user.id, exc)


def _clear_verification(session: Session, user: User) -> None:
    user.email_verification_token = None
    user.email_verification_token_expires_at = None
    discard_codes(session, user.id, CodePurpose.verify_email)


@router.post("/verify-email/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def verify_email_request(
    request: Request,
    body: EmailIn,
    session: Session = Depends(get_session),
) -> Response:
    """Resend a verification code. Always returns 204 to avoid revealing
    whether an account exists. Already-verified accounts are silently
    skipped — same response — so attackers can't probe verification state.
    """
    email = _norm_email(body.email)
    enforce_key_limits("verify_email-send", email, *_SEND_LIMITS)
    user = session.scalars(select(User).where(User.email == email)).first()
    if user and not user.email_verified:
        await _send_verification(session, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/verify-email/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
def verify_email_confirm(
    request: Request,
    body: VerifyEmailConfirmIn,
    session: Session = Depends(get_session),
) -> Response:
    if body.token:
        user = _user_from_link(
            session, User.email_verification_token,
            User.email_verification_token_expires_at, body.token,
        )
    else:
        user = _user_from_code(
            session, _norm_email(body.email), CodePurpose.verify_email, body.code,
        )
    user.email_verified = True
    user.email_verified_at = _now()
    _clear_verification(session, user)
    session.commit()
    logger.info("Email verified: user_id=%s", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── password reset ─────────────────────────────────────────────────────────────

def _clear_password_reset(session: Session, user: User) -> None:
    user.password_reset_token = None
    user.password_reset_token_expires_at = None
    discard_codes(session, user.id, CodePurpose.password_reset)


@router.post("/password-reset/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def password_reset_request(
    request: Request,
    body: EmailIn,
    session: Session = Depends(get_session),
) -> Response:
    """Always returns 204, regardless of whether the email exists, to avoid
    email enumeration. Codes expire after 15 minutes, legacy links after
    `password_reset_ttl_s` seconds.
    """
    email = _norm_email(body.email)
    enforce_key_limits("password_reset-send", email, *_SEND_LIMITS)
    user = session.scalars(select(User).where(User.email == email)).first()
    if user and user.is_active:
        settings = get_settings()
        code = issue_code(session, user.id, CodePurpose.password_reset, user.email)
        token = None
        if settings.legacy_email_links:
            token, user.password_reset_token = new_link_token()
            user.password_reset_token_expires_at = _now() + timedelta(
                seconds=settings.password_reset_ttl_s,
            )
        session.commit()
        try:
            await send_password_reset_email(to=user.email, code=code, token=token)
        except Exception as exc:
            logger.warning("Password-reset email send failed for user %s: %s", user.id, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password-reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/hour")
async def password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmIn,
    session: Session = Depends(get_session),
) -> Response:
    _check_new_password(body.new_password)  # before the code, so a typo costs no attempt
    if body.token:
        user = _user_from_link(
            session, User.password_reset_token,
            User.password_reset_token_expires_at, body.token,
        )
    else:
        user = _user_from_code(
            session, _norm_email(body.email), CodePurpose.password_reset, body.code,
        )
    user.hashed_password = hash_password(body.new_password)
    _clear_password_reset(session, user)
    revoke_all_refresh_tokens(session, user.id)
    session.commit()
    logger.info("Password reset: user_id=%s", user.id)
    await _notify(user.id, user.email, "Your whumpf password was reset.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── change password / email (signed in) ────────────────────────────────────────

@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour")
async def change_password(
    request: Request,
    body: ChangePasswordIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Revokes every refresh token except the family of ``refresh_token`` (if
    sent and live). Already-issued access tokens stay valid until expiry."""
    enforce_key_limits("change-password", str(user.id), "10/hour")
    _check_new_password(body.new_password)
    _check_current_password(user, body.current_password)
    keep = (
        live_refresh_family(session, user.id, body.refresh_token)
        if body.refresh_token else None
    )
    user.hashed_password = hash_password(body.new_password)
    _clear_password_reset(session, user)
    revoke_all_refresh_tokens(session, user.id, keep_family=keep)
    session.commit()
    logger.info("Password changed: user_id=%s", user.id)
    await _notify(user.id, user.email, "Your whumpf password was changed.")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-email/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def change_email_request(
    request: Request,
    body: ChangeEmailIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Email a code to the new address. The change happens on confirm."""
    new_email = _norm_email(body.new_email)
    enforce_key_limits("change_email-send", str(user.id), *_SEND_LIMITS)
    enforce_key_limits("change_email-send-to", new_email, *_SEND_LIMITS)
    if "@" not in new_email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid email address")
    if new_email == user.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That is already your email")
    # Password before the taken-check, so it can't probe for accounts without it.
    _check_current_password(user, body.current_password)
    if session.scalars(select(User.id).where(User.email == new_email)).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")
    code = issue_code(session, user.id, CodePurpose.change_email, new_email)
    session.commit()
    try:
        await send_email_change_code(to=new_email, code=code)
    except Exception as exc:
        logger.warning("Email-change code send failed for user %s: %s", user.id, exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-email/confirm", response_model=ChangeEmailOut)
@limiter.limit("20/hour")
async def change_email_confirm(
    request: Request,
    response: Response,
    body: CodeIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> ChangeEmailOut:
    """Switch to the address the code was sent to. Returns (and sets as the
    cookie) a new access token, since access tokens are keyed on email.
    Pending trip invites to the old address move to the new one."""
    enforce_key_limits("change_email-check", str(user.id), *_CHECK_LIMITS)
    try:
        row = check_code(session, user.id, CodePurpose.change_email, body.code)
    except CodeError as exc:
        session.commit()  # keep the used-up attempt
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE) from exc
    old_email, new_email = user.email, row.email
    taken = HTTPException(status.HTTP_400_BAD_REQUEST, "Email already registered")
    if session.scalars(select(User.id).where(User.email == new_email)).first():
        session.commit()
        raise taken

    user.email = new_email
    user.email_verified = True
    user.email_verified_at = _now()
    _clear_verification(session, user)
    session.execute(
        update(TripMember)
        .where(func.lower(TripMember.invited_email) == old_email.lower())
        .values(invited_email=new_email),
    )
    try:
        session.commit()
    except IntegrityError as exc:  # registered between the check and here
        session.rollback()
        raise taken from exc
    logger.info("Email changed: user_id=%s", user.id)
    await _notify(user.id, old_email, f"The email on your whumpf account was changed to {new_email}.")

    access = create_access_token(new_email)
    set_auth_cookie(response, access)
    return ChangeEmailOut(email=new_email, access_token=access)


# ── account deletion ───────────────────────────────────────────────────────────

def _delete_trip_rows(session: Session, trip: Trip) -> None:
    for model in (Waypoint, TripMember, TripRoute):
        for row in session.scalars(select(model).where(model.trip_id == trip.id)).all():
            session.delete(row)
    session.flush()
    session.delete(trip)


def _purge_user_data(session: Session, user: User) -> None:
    """Remove/reassign everything referencing ``user`` (no FK cascades exist).

    Owned trips go to the member who joined earliest (``responded_at``, falling
    back to ``invited_at``, then id), or are deleted if there is no other
    accepted member. Waypoints on surviving trips are reassigned to the trip's
    owner; children are flushed before their parents.
    """
    # Trips: transfer or delete.
    for trip in session.scalars(select(Trip).where(Trip.owner_id == user.id)).all():
        heir = session.scalars(
            select(TripMember)
            .where(
                TripMember.trip_id == trip.id,
                TripMember.status == MemberStatus.accepted,
                TripMember.user_id.is_not(None),
                TripMember.user_id != user.id,
            )
            .order_by(
                func.coalesce(TripMember.responded_at, TripMember.invited_at),
                TripMember.id,
            )
        ).first()
        if heir is None:
            _delete_trip_rows(session, trip)
        else:
            trip.owner_id = heir.user_id
            heir.role = MemberRole.owner

    session.flush()

    # Waypoints on surviving trips stay for the remaining party.
    for wp, trip_owner_id in session.execute(
        select(Waypoint, Trip.owner_id)
        .join(Trip, Trip.id == Waypoint.trip_id)
        .where(Waypoint.created_by_id == user.id)
    ).all():
        wp.created_by_id = trip_owner_id

    # Rows the user belongs to.
    session.execute(delete(TripMember).where(or_(
        TripMember.user_id == user.id,
        func.lower(TripMember.invited_email) == user.email.lower(),
    )))
    session.execute(delete(Friendship).where(or_(
        Friendship.requester_id == user.id, Friendship.addressee_id == user.id,
    )))

    # Routes: detach from trips and drop shares, then the routes themselves.
    route_ids = select(Route.id).where(Route.owner_id == user.id)
    session.execute(delete(TripRoute).where(TripRoute.route_id.in_(route_ids)))
    session.execute(delete(RouteShare).where(or_(
        RouteShare.route_id.in_(route_ids), RouteShare.created_by_id == user.id,
    )))
    session.execute(delete(Route).where(Route.owner_id == user.id))
    session.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
    session.execute(delete(EmailCode).where(EmailCode.user_id == user.id))
    session.flush()


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/hour")
async def delete_me(
    request: Request,
    response: Response,
    body: DeleteMeIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Self-serve account deletion, confirmed with the password. Best-effort
    server-side Strava revoke happens first; if it fails, the local user row
    is still deleted so the user isn't trapped by a dead upstream.
    """
    enforce_key_limits("delete-me", str(user.id), "10/hour")
    if not verify_password(body.password, user.hashed_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Password is incorrect")
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

    _purge_user_data(session, user)
    logger.info("Account deleted: user_id=%s", user.id)
    session.delete(user)
    session.commit()
    clear_auth_cookie(response)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
