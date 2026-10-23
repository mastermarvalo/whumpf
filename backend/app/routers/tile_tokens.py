"""POST /tiles/token — short-lived signed tokens for the /tiles/* endpoints."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.dependencies import get_optional_user
from app.models.user import User
from app.tile_tokens import TIER_ANON, TIER_USER, issue, limit_token_creation

router = APIRouter(prefix="/tiles", tags=["tiles"])


class TileTokenRequest(BaseModel):
    # Random id the app generates once and stores on the device.
    install_id: str | None = Field(None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class TileTokenResponse(BaseModel):
    token: str
    expires_at: datetime


@router.post("/token", response_model=TileTokenResponse)
def create_tile_token(
    request: Request,
    body: TileTokenRequest | None = Body(None),
    user: User | None = Depends(get_optional_user),
) -> TileTokenResponse:
    """Signed-in callers (Bearer or session cookie) get a `user` token;
    signed-out apps send their install id and get an `anon` token. Limited
    per user when signed in, per IP otherwise (tile_tokens.BUCKETS)."""
    limit_token_creation(request, user.id if user is not None else None)
    if user is not None:
        token, exp = issue(f"u:{user.id}", TIER_USER)
    elif body is not None and body.install_id:
        token, exp = issue(f"i:{body.install_id}", TIER_ANON)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="install_id is required when not signed in",
        )
    return TileTokenResponse(token=token, expires_at=datetime.fromtimestamp(exp, tz=UTC))
