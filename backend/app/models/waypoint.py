from __future__ import annotations

import enum
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class WaypointKind(str, enum.Enum):
    parking = "parking"
    trailhead = "trailhead"
    transition = "transition"
    decision = "decision"
    summit = "summit"
    hazard = "hazard"
    other = "other"


class Waypoint(Base):
    """A planning marker on a trip. Party-visible/editable (inherits trip
    membership). Geometry is a 3D point (Z = elevation, 0 if unknown)."""

    __tablename__ = "waypoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trip_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("trips.id"), index=True, nullable=False,
    )
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="POINTZ", srid=4326, spatial_index=True),
        nullable=False,
    )
    kind: Mapped[WaypointKind] = mapped_column(
        Enum(WaypointKind, native_enum=False, length=16),
        default=WaypointKind.other,
        server_default=WaypointKind.other.value,
        nullable=False,
    )
    label: Mapped[str] = mapped_column(String(255), default="", server_default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    created_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False,
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
