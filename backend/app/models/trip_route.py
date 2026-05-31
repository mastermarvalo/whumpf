from __future__ import annotations

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class TripRoute(Base):
    """Join table attaching routes to a trip, with an explicit ordering."""

    __tablename__ = "trip_routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trips.id"), index=True, nullable=False,
    )
    route_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("routes.id"), index=True, nullable=False,
    )
    # 1-based day of the trip this route belongs to (1 for single-day trips).
    day: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    ordering: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
