"""routes.strava_fetched_at: when an imported route's Strava data was fetched.

Strava's API Policy §6.2 allows keeping Strava data for at most 7 days, so the
strava-refresh job re-fetches imports before then (app/jobs/strava_refresh.py).
Existing imports are backfilled from created_at, the time they were fetched;
the first run refreshes them, or deletes those it can't.

docs/runbook.md §14.

Revision ID: 0004_strava_fetched_at
Revises: 0003_pack_requests
Create Date: 2026-10-23 19:29:29.813946
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0004_strava_fetched_at'
down_revision: str | None = '0003_pack_requests'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('routes', sa.Column('strava_fetched_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_routes_strava_fetched_at'), 'routes', ['strava_fetched_at'], unique=False)
    op.execute("UPDATE routes SET strava_fetched_at = created_at WHERE source_strava_id IS NOT NULL")


def downgrade() -> None:
    op.drop_index(op.f('ix_routes_strava_fetched_at'), table_name='routes')
    op.drop_column('routes', 'strava_fetched_at')
