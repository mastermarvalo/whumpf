"""Offline packs B3: pack_requests, packs.queued_at and packs.tile_bytes.

- pack_requests: who asked for which pack (POST /packs). New table.
- packs.queued_at: when a pack last went into the queue (the worker takes
  the oldest). Backfilled from created_at, then made NOT NULL.
- packs.tile_bytes: {layer: {zoom: [tiles, bytes]}} of the last build, for
  GET /packs/manifest. Existing rows get {}.

docs/runbook.md §12.

Revision ID: 0003_pack_requests
Revises: 0002_packs
Create Date: 2026-10-20 05:53:47.241841
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0003_pack_requests'
down_revision: str | None = '0002_packs'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('pack_requests',
    sa.Column('pack_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['pack_id'], ['packs.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('pack_id', 'user_id')
    )
    op.create_index('ix_pack_requests_user_id', 'pack_requests', ['user_id'], unique=False)
    op.add_column('packs', sa.Column('tile_bytes', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=False))
    op.add_column('packs', sa.Column('queued_at', sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE packs SET queued_at = created_at")
    op.alter_column('packs', 'queued_at', nullable=False)
    op.create_index('ix_packs_status_queued_at', 'packs', ['status', 'queued_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_packs_status_queued_at', table_name='packs')
    op.drop_column('packs', 'queued_at')
    op.drop_column('packs', 'tile_bytes')
    op.drop_index('ix_pack_requests_user_id', table_name='pack_requests')
    op.drop_table('pack_requests')
