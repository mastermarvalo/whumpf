from __future__ import annotations

import uuid
from datetime import UTC, datetime

from geoalchemy2 import Geometry
from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.user import Base

PACK_STATUSES = ("queued", "building", "ready", "failed")


class Pack(Base):
    """An offline pack: one PMTiles file per layer for a box, built by the
    pack worker (app/jobs/pack_worker.py) into R2 at packs/<id>/<layer>.pmtiles.
    docs/runbook.md §12.
    """

    __tablename__ = "packs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'building', 'ready', 'failed')", name="ck_packs_status",
        ),
        Index("ix_packs_status_created_at", "status", "created_at"),
    )

    # A UUID, so packs/<id>/ in R2 can't be guessed.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    # The box as asked for; the files cover it rounded out to the zoom 14 grid.
    bbox = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=False), nullable=False,
    )
    layers: Mapped[list[str]] = mapped_column(ARRAY(String(16)), nullable=False)
    # {layer: version} when built; null for the USGS layers (kept forever).
    source_versions: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    # Builds started. A build cut off by a crash counts; after 3 it fails.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tiles_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tiles_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{layer, version, bytes, md5, key}]; a layer with no tiles in the box
    # has bytes 0 and md5/key null.
    files: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="[]")
    # Who first asked for it. Plain int, no FK (like status_banners); set to
    # null when that account is deleted.
    requested_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
    )
    built_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False,
    )
