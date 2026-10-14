from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Response
from jose import JWTError, jwt
from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.refresh_token import RefreshToken
from app.models.user import User

logger = logging.getLogger("whumpf.auth")

# Browsers store the JWT in this httpOnly cookie. The cookie is the primary
# auth mechanism for the SPA; we keep an OAuth2 Bearer fallback for the
# FastAPI docs UI and scripted clients in app/auth/dependencies.py.
SESSION_COOKIE = "whumpf_session"


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(subject: str) -> str:
    s = get_settings()
    exp = datetime.now(timezone.utc) + timedelta(minutes=s.jwt_access_ttl_min)
    return jwt.encode({"sub": subject, "exp": exp}, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> str | None:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
        return payload.get("sub")
    except JWTError:
        return None


# ── refresh tokens ─────────────────────────────────────────────────────────────

# A token rotated less than this long ago may be presented again without
# counting as reuse: covers a client that retries a refresh whose response was
# lost, or two refreshes racing.
REFRESH_REUSE_GRACE_S = 60


class RefreshTokenError(Exception):
    """Refresh token unknown, expired, revoked, or its user can't sign in."""


def _hash_refresh_token(raw: str) -> str:
    # Plain SHA-256 is enough: the token is 256 random bits, and we need a
    # deterministic hash to look it up.
    return hashlib.sha256(raw.encode()).hexdigest()


def issue_refresh_token(session: Session, user_id: int, family_id: str | None = None) -> str:
    """Store a new refresh token and return its plaintext. A new login starts a
    new family; rotations pass the old token's family. Caller commits."""
    raw = secrets.token_urlsafe(32)
    session.add(RefreshToken(
        user_id=user_id,
        family_id=family_id or secrets.token_hex(16),
        token_hash=_hash_refresh_token(raw),
        expires_at=datetime.now(timezone.utc)
        + timedelta(days=get_settings().refresh_token_ttl_days),
    ))
    return raw


def _can_sign_in(user: User | None) -> bool:
    if not user or not user.is_active:
        return False
    return get_settings().skip_email_verification or user.email_verified


def rotate_refresh_token(session: Session, raw: str) -> tuple[User, str]:
    """Swap a refresh token for a new one in the same family; revoke the old.

    Presenting a token that a refresh already rotated is reuse (it may have
    been stolen): every refresh token the user holds is revoked. Exception: a
    token rotated within REFRESH_REUSE_GRACE_S whose family still has a live
    token gets a fresh token in that family instead, and the live one stays
    valid. A token revoked any other way (logout, password reset, reuse
    detection) is simply rejected.

    Raises RefreshTokenError. Caller commits — also on error, so a reuse
    revocation sticks.
    """
    now = datetime.now(timezone.utc)
    # FOR UPDATE: two concurrent refreshes with the same token serialize here,
    # so the second sees the first's revocation (and takes the grace path).
    row = session.scalars(
        select(RefreshToken)
        .where(RefreshToken.token_hash == _hash_refresh_token(raw))
        .with_for_update(),
    ).first()
    if row is None or row.expires_at <= now:
        raise RefreshTokenError("Invalid or expired refresh token")

    if row.revoked_at is not None and row.rotated_at is None:
        raise RefreshTokenError("Invalid or expired refresh token")

    if row.rotated_at is not None:
        family_live = session.scalar(select(exists().where(
            RefreshToken.family_id == row.family_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )))
        if not (family_live and row.rotated_at > now - timedelta(seconds=REFRESH_REUSE_GRACE_S)):
            revoke_all_refresh_tokens(session, row.user_id)
            logger.warning("Refresh token reuse: user_id=%s; all refresh tokens revoked",
                           row.user_id)
            raise RefreshTokenError("Invalid or expired refresh token")

    user = session.get(User, row.user_id)
    if not _can_sign_in(user):
        raise RefreshTokenError("Invalid or expired refresh token")
    if row.revoked_at is None:
        row.revoked_at = row.rotated_at = now
    return user, issue_refresh_token(session, user.id, row.family_id)


def revoke_refresh_family(session: Session, raw: str) -> None:
    """Sign one device out: revoke the presented token's whole family. Unknown
    tokens are ignored. Caller commits."""
    family_id = session.scalar(
        select(RefreshToken.family_id)
        .where(RefreshToken.token_hash == _hash_refresh_token(raw)),
    )
    if family_id is None:
        return
    session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc)),
    )


def revoke_all_refresh_tokens(
    session: Session, user_id: int, keep_family: str | None = None,
) -> None:
    """Revoke every live refresh token of ``user_id``, except those in
    ``keep_family`` if given. Caller commits."""
    stmt = (
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    if keep_family is not None:
        stmt = stmt.where(RefreshToken.family_id != keep_family)
    session.execute(stmt)


def live_refresh_family(session: Session, user_id: int, raw: str) -> str | None:
    """Family of ``raw`` if it is a live refresh token of ``user_id``."""
    return session.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == _hash_refresh_token(raw),
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(timezone.utc),
        ),
    )


def set_auth_cookie(response: Response, token: str) -> None:
    """Write the session JWT as an httpOnly cookie.

    httponly  — JavaScript (and therefore XSS) cannot read it.
    secure    — disabled in dev so HTTP localhost works; mandatory in prod.
    samesite=lax — sent on top-level GET navigations (needed for Strava
                   OAuth callback) but blocked on cross-site POST/DELETE.
    """
    s = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        httponly=True,
        secure=s.whumpf_env != "dev",
        samesite="lax",
        max_age=s.jwt_access_ttl_min * 60,
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
