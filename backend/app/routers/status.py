"""Public status page + admin banner management.

GET /status is the only unauthenticated endpoint here — everything else
requires admin (see app.admin.require_admin). The page is meant to be
linkable from anywhere, including the marketing site / outage notices.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.admin import is_admin, require_admin
from app.auth.dependencies import get_current_user
from app.db import get_session
from app.models.status import StatusBanner
from app.models.user import User

router = APIRouter(prefix="/status", tags=["status"])
logger = logging.getLogger("whumpf.status")

# 3s timeout — these are local-network pings, anything slower means trouble.
_HTTP = httpx.AsyncClient(timeout=3.0)


# ── service probes ─────────────────────────────────────────────────────────────

class ServiceHealth(BaseModel):
    name: str
    healthy: bool
    detail: str | None = None


async def _check_postgres(session: Session) -> ServiceHealth:
    try:
        session.execute(text("SELECT 1"))
        return ServiceHealth(name="postgres", healthy=True)
    except Exception as exc:
        return ServiceHealth(name="postgres", healthy=False, detail=str(exc)[:200])


async def _probe(name: str, url: str) -> ServiceHealth:
    try:
        r = await _HTTP.get(url)
        return ServiceHealth(name=name, healthy=r.status_code < 500)
    except Exception as exc:
        return ServiceHealth(name=name, healthy=False, detail=str(exc)[:200])


async def _check_services(session: Session) -> list[ServiceHealth]:
    # api is implicitly healthy if it's responding to this request, but we
    # report it explicitly so the frontend doesn't have to know that.
    api = ServiceHealth(name="api", healthy=True)
    postgres = await _check_postgres(session)
    minio, titiler, martin = await asyncio.gather(
        _probe("minio",   "http://localhost:9000/minio/health/live"),
        _probe("titiler", "http://localhost:8001/"),
        _probe("martin",  "http://localhost:3000/health"),
    )
    return [api, postgres, minio, titiler, martin]


def _overall(services: list[ServiceHealth], active_banner: StatusBanner | None) -> str:
    """One-word summary the status page header uses for the big indicator.

    A posted critical banner forces "outage" regardless of probes; a major
    banner forces at-least "degraded". This way an admin can communicate
    user-visible breakage that probes don't catch (e.g. third-party SNOTEL
    down).
    """
    down = sum(1 for s in services if not s.healthy)
    if active_banner and active_banner.severity == "critical":
        return "outage"
    if down >= 2 or (active_banner and active_banner.severity == "major"):
        return "degraded"
    if down == 1:
        return "degraded"
    return "operational"


# ── schemas ────────────────────────────────────────────────────────────────────

class BannerOut(BaseModel):
    id: int
    title: str
    body: str
    severity: str
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


class StatusOut(BaseModel):
    overall: str  # "operational" | "degraded" | "outage"
    services: list[ServiceHealth]
    active_banner: BannerOut | None
    recent_banners: list[BannerOut]


class BannerIn(BaseModel):
    title: str
    body: str = ""
    severity: str = "minor"  # "minor" | "major" | "critical"


# ── public endpoint ────────────────────────────────────────────────────────────

@router.get("", response_model=StatusOut)
async def get_status(session: Session = Depends(get_session)) -> StatusOut:
    services = await _check_services(session)
    active = session.scalars(
        select(StatusBanner)
        .where(StatusBanner.resolved_at.is_(None))
        .order_by(StatusBanner.created_at.desc())
    ).first()
    recent = list(session.scalars(
        select(StatusBanner)
        .order_by(StatusBanner.created_at.desc())
        .limit(10)
    ).all())
    return StatusOut(
        overall=_overall(services, active),
        services=services,
        active_banner=BannerOut.model_validate(active) if active else None,
        recent_banners=[BannerOut.model_validate(b) for b in recent],
    )


# ── admin endpoints ────────────────────────────────────────────────────────────

_ALLOWED_SEVERITIES = {"minor", "major", "critical"}


@router.post("", response_model=BannerOut, status_code=status.HTTP_201_CREATED)
def post_banner(
    body: BannerIn,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> BannerOut:
    if body.severity not in _ALLOWED_SEVERITIES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"severity must be one of {sorted(_ALLOWED_SEVERITIES)}",
        )
    if not body.title.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "title cannot be empty")
    banner = StatusBanner(
        title=body.title.strip(),
        body=body.body,
        severity=body.severity,
        created_by_id=admin.id,
    )
    session.add(banner)
    session.commit()
    session.refresh(banner)
    logger.info("status banner posted: id=%s severity=%s by=%s",
                banner.id, banner.severity, admin.id)
    return BannerOut.model_validate(banner)


@router.post("/{banner_id}/resolve", response_model=BannerOut)
def resolve_banner(
    banner_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> BannerOut:
    banner = session.get(StatusBanner, banner_id)
    if not banner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Banner not found")
    if banner.resolved_at is None:
        banner.resolved_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(banner)
        logger.info("status banner resolved: id=%s by=%s", banner.id, admin.id)
    return BannerOut.model_validate(banner)


@router.delete("/{banner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_banner(
    banner_id: int,
    admin: User = Depends(require_admin),
    session: Session = Depends(get_session),
) -> None:
    banner = session.get(StatusBanner, banner_id)
    if not banner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Banner not found")
    session.delete(banner)
    session.commit()
    logger.info("status banner deleted: id=%s by=%s", banner_id, admin.id)


# ── helper used by /auth/me to surface is_admin without a separate fetch ──────

def current_user_is_admin(user: User = Depends(get_current_user)) -> bool:
    """Convenience dependency for /auth/me — tells the frontend if it should
    even bother offering admin UI."""
    return is_admin(user)
