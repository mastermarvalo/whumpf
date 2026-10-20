"""Offline packs: the packs table the pack worker builds from (B2).

New table only; nothing to backfill. docs/runbook.md §12.

Revision ID: 0002_packs
Revises: 0001_baseline
Create Date: 2026-10-20 05:14:24.191798
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision: str = '0002_packs'
down_revision: str | None = '0001_baseline'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_geospatial_table('packs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('region', sa.String(length=32), nullable=False),
    sa.Column('bbox', Geometry(geometry_type='POLYGON', srid=4326, dimension=2, spatial_index=False, from_text='ST_GeomFromEWKT', name='geometry', nullable=False), nullable=False),
    sa.Column('layers', postgresql.ARRAY(sa.String(length=16)), nullable=False),
    sa.Column('source_versions', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False),
    sa.Column('status', sa.String(length=16), server_default='queued', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tiles_done', sa.Integer(), server_default='0', nullable=False),
    sa.Column('tiles_total', sa.Integer(), server_default='0', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('files', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
    sa.Column('requested_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('built_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'building', 'ready', 'failed')", name='ck_packs_status'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_packs_status_created_at', 'packs', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_packs_status_created_at', table_name='packs')
    op.drop_geospatial_table('packs')
