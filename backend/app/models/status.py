from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class StatusBanner(Base):
    """A single status-page incident.

    Lifecycle: created → (optionally edited) → resolved (resolved_at set).
    Unresolved banners are 'active' and surface as the prominent message
    on /status; resolved ones appear in the recent-history list.
    """

    __tablename__ = "status_banners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    # "minor" | "major" | "critical" — drives the banner color on the page.
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="minor", server_default="minor",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Audit trail: who posted this. Plain int — no FK so deleting the author
    # account doesn't cascade and lose history. Nullable because of that.
    created_by_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
