"""Outage banners on GET /status: posting, resolving, listing.

Shared by the admin endpoints (app/routers/status.py) and `make banner`
(app/jobs/banner.py), so both check banners the same way.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.status import StatusBanner

SEVERITIES = ("minor", "major", "critical")
TITLE_MAX = 200  # the column is String(200)


def check(title: str, severity: str) -> str:
    """The stripped title; ValueError with a message for the user if the
    banner can't be posted."""
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {sorted(SEVERITIES)}")
    title = title.strip()
    if not title:
        raise ValueError("title cannot be empty")
    if len(title) > TITLE_MAX:
        raise ValueError(f"title is {len(title)} characters; the most is {TITLE_MAX}")
    return title


def post(
    session: Session, *, title: str, body: str = "", severity: str = "minor",
    created_by_id: int | None,
) -> StatusBanner:
    """Post (and commit) a banner. ValueError if `check` fails."""
    banner = StatusBanner(
        title=check(title, severity), body=body, severity=severity, created_by_id=created_by_id,
    )
    session.add(banner)
    session.commit()
    session.refresh(banner)
    return banner


def resolve(session: Session, banner_id: int) -> tuple[StatusBanner | None, bool]:
    """(banner, changed): None if there is no such banner; changed is False
    when it was already resolved."""
    banner = session.get(StatusBanner, banner_id)
    if banner is None or banner.resolved_at is not None:
        return banner, False
    banner.resolved_at = datetime.now(UTC)
    session.commit()
    session.refresh(banner)
    return banner, True


def active(session: Session) -> list[StatusBanner]:
    """Unresolved banners, newest first. /status shows the newest one."""
    return list(session.scalars(
        select(StatusBanner)
        .where(StatusBanner.resolved_at.is_(None))
        .order_by(StatusBanner.created_at.desc(), StatusBanner.id.desc())
    ))


def recent(session: Session, limit: int = 10) -> list[StatusBanner]:
    return list(session.scalars(
        select(StatusBanner).order_by(StatusBanner.created_at.desc(), StatusBanner.id.desc()).limit(limit)
    ))
