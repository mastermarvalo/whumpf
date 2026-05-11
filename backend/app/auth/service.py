from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Response
from jose import JWTError, jwt

from app.config import get_settings

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
