from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class FriendStatus(str, enum.Enum):
    pending = "pending"
    accepted = "accepted"


class Friendship(Base):
    """A mutual friendship between two users. The requester sends a request
    (``pending``); the addressee accepts (``accepted``) or declines (row deleted).
    Two users are friends iff an ``accepted`` row exists in either direction.

    Friends are added by email only (no name search / discovery) — a private
    party graph, not a social feed.
    """

    __tablename__ = "friendships"
    __table_args__ = (
        UniqueConstraint("requester_id", "addressee_id", name="uq_friendship_pair"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False,
    )
    addressee_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False,
    )
    status: Mapped[FriendStatus] = mapped_column(
        Enum(FriendStatus, native_enum=False, length=16),
        default=FriendStatus.pending,
        server_default=FriendStatus.pending.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
