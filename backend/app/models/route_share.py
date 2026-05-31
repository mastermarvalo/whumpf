from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class RouteShare(Base):
    """A revocable share token granting view + clone on a route to any logged-in
    holder of the link. The token table is the source of truth for share access
    (the vestigial `routes.share_token` column is unused). An "active" share is
    one with ``revoked_at IS NULL``.
    """

    __tablename__ = "route_shares"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    route_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("routes.id"), index=True, nullable=False,
    )
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
