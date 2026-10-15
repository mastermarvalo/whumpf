"""Baseline: the schema as it stood when Alembic was introduced.

Equivalent to the old ``Base.metadata.create_all()`` plus every statement in
the retired ``_SCHEMA_PATCHES`` tuple. Columns are listed in the same physical
order as the production database (patched-in columns sit at the end of their
tables), so ``make schema-diff`` compares cleanly.

The production database pre-dates this file and was *stamped* at this
revision rather than upgraded — see docs/runbook.md "Schema migrations".

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-14
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The only database a downgrade of this revision may run against without an
# explicit override. Downgrading drops every table.
DISPOSABLE_DATABASE = "whumpf_test"

_EXTENSIONS = ("btree_gist", "pg_trgm", "postgis", "postgis_raster", "postgis_topology")


def _tz() -> sa.DateTime:
    return sa.DateTime(timezone=True)


def check_upgrade_allowed(existing_tables: set[str]) -> None:
    """Refuse to build the baseline over a database that already has the
    schema but was never stamped (e.g. prod before the Alembic switch)."""
    if "users" in existing_tables:
        raise RuntimeError(
            "Database already has Whumpf tables but no alembic_version row. "
            "Do not upgrade it; verify with `make schema-diff`, then "
            "`make alembic ARGS=\"stamp 0001_baseline\"`. See docs/runbook.md."
        )


def check_downgrade_allowed(database: str) -> None:
    """Refuse to drop every table unless this is the test database or the
    operator set ALLOW_DESTRUCTIVE_DOWNGRADE=1 for this one command."""
    if database == DISPOSABLE_DATABASE:
        return
    if os.environ.get("ALLOW_DESTRUCTIVE_DOWNGRADE") == "1":
        return
    raise RuntimeError(
        f"Refusing to downgrade 0001_baseline on database {database!r}: it drops "
        f"every table. Only {DISPOSABLE_DATABASE!r} is allowed unless "
        "ALLOW_DESTRUCTIVE_DOWNGRADE=1 is set."
    )


def upgrade() -> None:
    bind = op.get_bind()
    check_upgrade_allowed(set(sa.inspect(bind).get_table_names()))

    # Normally created by scripts/postgis-init.sql on first boot; repeated here
    # so a database built purely from migrations is complete.
    for ext in _EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("email_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("email_verified_at", _tz(), nullable=True),
        sa.Column("email_verification_token", sa.String(64), nullable=True),
        sa.Column("email_verification_token_expires_at", _tz(), nullable=True),
        sa.Column("password_reset_token", sa.String(64), nullable=True),
        sa.Column("password_reset_token_expires_at", _tz(), nullable=True),
        sa.Column("allowed_regions", sa.String(255), server_default="colorado", nullable=False),
        sa.Column("is_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_email_verification_token", "users", ["email_verification_token"])
    op.create_index("ix_users_password_reset_token", "users", ["password_reset_token"])

    op.create_table(
        "email_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("purpose", sa.String(20), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", _tz(), nullable=False),
        sa.Column("consumed_at", _tz(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
    )
    op.create_index("ix_email_codes_user_id", "email_codes", ["user_id"])

    op.create_table(
        "friendships",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requester_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("addressee_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("responded_at", _tz(), nullable=True),
        sa.UniqueConstraint("requester_id", "addressee_id", name="uq_friendship_pair"),
    )
    op.create_index("ix_friendships_addressee_id", "friendships", ["addressee_id"])
    op.create_index("ix_friendships_requester_id", "friendships", ["requester_id"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("family_id", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", _tz(), nullable=False),
        sa.Column("revoked_at", _tz(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("rotated_at", _tz(), nullable=True),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_token_hash", "refresh_tokens", ["token_hash"], unique=True)
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "routes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        # spatial_index=False: the GiST index is created explicitly below so
        # its name (idx_routes_geom) is pinned in this file.
        sa.Column(
            "geom",
            Geometry(geometry_type="LINESTRINGZ", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("summary", JSONB(), nullable=False),
        sa.Column("samples", JSONB(), nullable=False),
        sa.Column("visibility", sa.String(16), server_default="private", nullable=False),
        sa.Column("share_token", sa.String(64), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.Column("source_strava_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("idx_routes_geom", "routes", ["geom"], postgresql_using="gist")
    op.create_index("ix_routes_owner_id", "routes", ["owner_id"])
    op.create_index("ix_routes_share_token", "routes", ["share_token"])
    op.create_index("ix_routes_source_strava_id", "routes", ["source_strava_id"])

    op.create_table(
        "route_shares",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.id"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("revoked_at", _tz(), nullable=True),
    )
    op.create_index("ix_route_shares_route_id", "route_shares", ["route_id"])
    op.create_index("ix_route_shares_token", "route_shares", ["token"], unique=True)

    op.create_table(
        "status_banners",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), server_default="", nullable=False),
        sa.Column("severity", sa.String(16), server_default="minor", nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("resolved_at", _tz(), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
    )

    op.create_table(
        "strava_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("strava_athlete_id", sa.Integer(), nullable=False),
        # EncryptedString columns: ciphertext stored as VARCHAR(255).
        sa.Column("access_token", sa.String(255), nullable=False),
        sa.Column("refresh_token", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(255), nullable=False),
        sa.Column("athlete_name", sa.String(255), nullable=False),
        sa.Column("athlete_icon_url", sa.String(512), nullable=True),
        sa.Column("connected_at", _tz(), nullable=False),
    )
    op.create_index(
        "ix_strava_connections_strava_athlete_id", "strava_connections",
        ["strava_athlete_id"], unique=True,
    )

    op.create_table(
        "trips",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("caic_zone", sa.String(128), nullable=True),
        sa.Column("forecast_snapshot", JSONB(), nullable=True),
        sa.Column("visibility", sa.String(16), server_default="private", nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.Column("num_days", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_trips_owner_id", "trips", ["owner_id"])

    op.create_table(
        "trip_members",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.Integer(), sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("invited_email", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), server_default="invited", nullable=False),
        sa.Column("role", sa.String(16), server_default="member", nullable=False),
        sa.Column("invited_at", _tz(), nullable=False),
        sa.Column("responded_at", _tz(), nullable=True),
    )
    op.create_index("ix_trip_members_invited_email", "trip_members", ["invited_email"])
    op.create_index("ix_trip_members_trip_id", "trip_members", ["trip_id"])
    op.create_index("ix_trip_members_user_id", "trip_members", ["user_id"])

    op.create_table(
        "trip_routes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.Integer(), sa.ForeignKey("trips.id"), nullable=False),
        sa.Column("route_id", sa.Integer(), sa.ForeignKey("routes.id"), nullable=False),
        sa.Column("ordering", sa.Integer(), server_default="0", nullable=False),
        sa.Column("day", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index("ix_trip_routes_route_id", "trip_routes", ["route_id"])
    op.create_index("ix_trip_routes_trip_id", "trip_routes", ["trip_id"])

    op.create_table(
        "waypoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("trip_id", sa.Integer(), sa.ForeignKey("trips.id"), nullable=False),
        sa.Column(
            "geom",
            Geometry(geometry_type="POINTZ", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), server_default="other", nullable=False),
        sa.Column("label", sa.String(255), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.Column("color", sa.String(7), nullable=True),
    )
    op.create_index("idx_waypoints_geom", "waypoints", ["geom"], postgresql_using="gist")
    op.create_index("ix_waypoints_trip_id", "waypoints", ["trip_id"])


def downgrade() -> None:
    check_downgrade_allowed(op.get_bind().exec_driver_sql("SELECT current_database()").scalar_one())

    # Children before parents (no FK cascades). Extensions are left in place.
    for table in (
        "waypoints", "trip_routes", "trip_members", "trips", "strava_connections",
        "status_banners", "route_shares", "routes", "refresh_tokens", "friendships",
        "email_codes", "users",
    ):
        op.drop_table(table)
