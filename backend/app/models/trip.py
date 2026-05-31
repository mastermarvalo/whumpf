from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.route import Visibility
from app.models.user import Base


class Trip(Base):
    """A dated plan: one or more routes + a party + a frozen CAIC forecast.

    ``forecast_snapshot`` is a frozen copy of the CAIC zone forecast captured at
    trip creation (the CaicZoneDetail shape) — never recomputed; the point is
    "what we knew when we planned." Access is membership-based (see TripMember);
    the ``visibility`` enum is reserved/cosmetic for now.
    """

    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # ``date`` is the start day; a trip spans ``num_days`` consecutive days
    # (1 = single-day). Per-day route assignment lives on TripRoute.day.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    num_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)

    # CAIC zone id/name this trip is in, and the frozen forecast for it.
    caic_zone: Mapped[str | None] = mapped_column(String(128), nullable=True)
    forecast_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, native_enum=False, length=16),
        default=Visibility.private,
        server_default=Visibility.private.value,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
