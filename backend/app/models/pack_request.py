from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class PackRequest(Base):
    """A user asked for a pack (POST /packs). Packs are shared: one row per
    user per pack. GET /packs/{id} answers only users with a row here, and a
    user's queued or building packs are counted from these rows. No
    cascades: account deletion and the pack cleanup delete these first.
    """

    __tablename__ = "pack_requests"
    __table_args__ = (Index("ix_pack_requests_user_id", "user_id"),)

    pack_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("packs.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
    )
