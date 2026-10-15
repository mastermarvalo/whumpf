#!/bin/bash
# Compare the live database schema with one built purely from migrations.
#
#   make schema-diff
#
# 1. (Re)creates the scratch database whumpf_schemacheck on the same server.
# 2. Runs `alembic upgrade head` into it from a one-off api container (the
#    current image, i.e. what `make build-api` produced — not the running api).
# 3. pg_dump --schema-only of both, minus the alembic_version table (it exists
#    only once a database is stamped), then diff.
# 4. Drops the scratch database.
#
# Exit 0 = identical. Only the scratch database is ever created or dropped;
# the live database is only read.

set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=${COMPOSE:-$HOME/.docker/cli-plugins/docker-compose}
PGUSER=${POSTGRES_USER:-whumpf}
LIVE_DB=${POSTGRES_DB:-whumpf}
SCRATCH_DB=whumpf_schemacheck

WORK=$(mktemp -d)
psql_admin() {
    $COMPOSE exec -T postgis psql -v ON_ERROR_STOP=1 -q -U "$PGUSER" -d postgres -c "$1"
}
cleanup() {
    psql_admin "DROP DATABASE IF EXISTS $SCRATCH_DB WITH (FORCE)" || true
    rm -rf "$WORK"
}
trap cleanup EXIT

schema_dump() {
    # Strip comments, SET/search_path noise and blank lines; nothing else.
    $COMPOSE exec -T postgis pg_dump -U "$PGUSER" -d "$1" \
        --schema-only --no-owner --no-privileges --exclude-table=public.alembic_version \
        | grep -vE '^(--|SET |SELECT pg_catalog\.set_config|\\(un)?restrict |$)'
}

echo "→ building $SCRATCH_DB from migrations"
psql_admin "DROP DATABASE IF EXISTS $SCRATCH_DB WITH (FORCE)"
psql_admin "CREATE DATABASE $SCRATCH_DB"
$COMPOSE run --rm --no-deps -T api uv run alembic -x dbname=$SCRATCH_DB upgrade head

echo "→ dumping schemas"
schema_dump "$LIVE_DB" > "$WORK/live.sql"
schema_dump "$SCRATCH_DB" > "$WORK/migrations.sql"

if diff -u --label "live:$LIVE_DB" --label "migrations:head" "$WORK/live.sql" "$WORK/migrations.sql"; then
    echo "✓ schema identical: $LIVE_DB == migrations head ($(wc -l < "$WORK/live.sql") lines compared)"
else
    echo "✗ schema differs (see diff above)" >&2
    exit 1
fi
