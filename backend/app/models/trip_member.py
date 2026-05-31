from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class MemberStatus(str, enum.Enum):
    invited = "invited"
    accepted = "accepted"
    declined = "declined"


class MemberRole(str, enum.Enum):
    owner = "owner"
    member = "member"


class TripMember(Base):
    """Party membership for a trip. Invited by email; ``user_id`` is filled in
    once a registered user accepts (or at invite time if the email is already a
    known user). The owner is auto-added as ``accepted`` / ``owner`` on create.
    """

    __tablename__ = "trip_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trips.id"), index=True, nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=True,
    )
    invited_email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    status: Mapped[MemberStatus] = mapped_column(
        Enum(MemberStatus, native_enum=False, length=16),
        default=MemberStatus.invited,
        server_default=MemberStatus.invited.value,
        nullable=False,
    )
    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole, native_enum=False, length=16),
        default=MemberRole.member,
        server_default=MemberRole.member.value,
        nullable=False,
    )
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
