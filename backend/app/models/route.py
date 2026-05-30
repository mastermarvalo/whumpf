from __future__ import annotations

import enum
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base


class Visibility(str, enum.Enum):
    """Shared visibility enum for all planning objects. Deliberately no
    ``public`` value — Whumpf is a planning tool, not a social network.

    - ``private``  — owner only.
    - ``shared``   — explicit party members / share-token holders (Phase B).
    - ``unlisted`` — anyone holding the link token, never discoverable.
    """

    private = "private"
    shared = "shared"
    unlisted = "unlisted"


class Route(Base):
    """A reusable, owner-scoped polyline with a cached terrain profile.

    The geometry is the polyline the user drew (Z = DEM elevation per vertex).
    The full per-point profile lives in ``samples`` (a few KB of JSONB) so a
    saved route can render its chart and histograms without re-reading COGs;
    ``summary`` holds the aggregate ProfileSummary shape.
    """

    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), index=True, nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    region: Mapped[str] = mapped_column(String(64), nullable=False)

    # PostGIS geometry — 3D line, WGS84. spatial_index builds a GiST index.
    geom: Mapped[object] = mapped_column(
        Geometry(geometry_type="LINESTRINGZ", srid=4326, spatial_index=True),
        nullable=False,
    )

    summary: Mapped[dict] = mapped_column(JSONB, nullable=False)
    samples: Mapped[list] = mapped_column(JSONB, nullable=False)

    # native_enum=False → stored as VARCHAR + CHECK constraint, so there's no
    # Postgres enum type to migrate (we have no Alembic).
    visibility: Mapped[Visibility] = mapped_column(
        Enum(Visibility, native_enum=False, length=16),
        default=Visibility.private,
        server_default=Visibility.private.value,
        nullable=False,
    )
    # Set only when visibility == unlisted (token-based access — Phase B).
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    # Strava activity id this route was imported from, if any. Used to dedupe
    # imports so the same activity can't be saved twice by the same owner.
    # BIGINT — Strava activity ids exceed 32-bit. Indexed for the dedupe lookup.
    source_strava_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True,
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
