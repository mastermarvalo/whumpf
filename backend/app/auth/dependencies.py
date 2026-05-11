from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.service import SESSION_COOKIE, decode_token
from app.db import get_session
from app.models.user import User

# auto_error=False so missing Authorization is not fatal — we may have a
# session cookie instead. The OAuth2 scheme is kept primarily for the FastAPI
# /docs Authorize button and any scripted (non-browser) clients.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


def get_current_user(
    request: Request,
    bearer_token: str | None = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> User:
    # Cookie takes precedence: browser sessions always carry it, and we don't
    # want an attacker-controlled Authorization header to override a valid
    # session for the same request.
    token = request.cookies.get(SESSION_COOKIE) or bearer_token
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    email = decode_token(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = session.scalars(select(User).where(User.email == email)).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
