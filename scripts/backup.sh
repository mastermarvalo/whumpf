#!/bin/bash
# Whumpf backup — daily snapshot to any S3-compatible bucket.
#
# Dumps:
#   * Postgres (everything — users, strava_connections, …)
#   * MinIO `user-uploads` bucket (GPX, photos)
#   * Optionally `dem-cogs` (set BACKUP_INCLUDE_COGS=true; multi-GB)
#
# Skips:
#   * `dem-raw` — redownloadable from USGS 3DEP, no point paying to store it
#   * `strava-cache` — regenerable
#
# Retention: postgres dumps older than 30 days are pruned; user-uploads is
# mirrored without --remove so deletions on the source don't wipe the backup.
#
# Schedule via cron (3am UTC daily):
#   0 3 * * * /home/ronkerflonk/whumpf/scripts/backup.sh >> /var/log/whumpf-backup.log 2>&1
#
# Configure: set BACKUP_S3_* in .env. See docs/external-services.md.

set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=/dev/null
set -a; . ./.env; set +a

if [ -z "${BACKUP_S3_ENDPOINT:-}" ] || [ -z "${BACKUP_S3_BUCKET:-}" ] \
   || [ -z "${BACKUP_S3_ACCESS_KEY:-}" ] || [ -z "${BACKUP_S3_SECRET_KEY:-}" ]; then
    echo "BACKUP_S3_* env vars not configured — nothing to do."
    echo "See docs/external-services.md → Backups."
    exit 0
fi

TIMESTAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "[$(date -u +%FT%TZ)] whumpf backup $TIMESTAMP starting"

# ── postgres dump ─────────────────────────────────────────────────────────────
echo "  → dumping postgres"
podman exec whumpf-postgis pg_dump \
    -U "${POSTGRES_USER:-whumpf}" \
    -d "${POSTGRES_DB:-whumpf}" \
    --clean --if-exists \
    | gzip > "$WORK/postgres-$TIMESTAMP.sql.gz"

DUMP_SIZE=$(du -h "$WORK/postgres-$TIMESTAMP.sql.gz" | cut -f1)
echo "    pg_dump: $DUMP_SIZE"

# ── upload via mc (one-shot container, no host install) ───────────────────────
MIRROR_COGS=""
if [ "${BACKUP_INCLUDE_COGS:-false}" = "true" ]; then
    MIRROR_COGS="mc mirror --overwrite local/dem-cogs backup/$BACKUP_S3_BUCKET/dem-cogs/ &&"
fi

echo "  → uploading to $BACKUP_S3_ENDPOINT/$BACKUP_S3_BUCKET"
podman run --rm --network host \
    -e BACKUP_S3_ENDPOINT \
    -e BACKUP_S3_BUCKET \
    -e BACKUP_S3_ACCESS_KEY \
    -e BACKUP_S3_SECRET_KEY \
    -e MINIO_ROOT_USER \
    -e MINIO_ROOT_PASSWORD \
    -v "$WORK:/work:Z" \
    quay.io/minio/mc:latest sh -c '
        set -e
        mc alias set local  http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
        mc alias set backup "$BACKUP_S3_ENDPOINT" "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY" >/dev/null
        mc cp /work/postgres-*.sql.gz "backup/$BACKUP_S3_BUCKET/postgres/" >/dev/null
        mc mirror --overwrite local/user-uploads "backup/$BACKUP_S3_BUCKET/user-uploads/" >/dev/null
        '"$MIRROR_COGS"'
        # 30-day retention on postgres dumps. user-uploads is mirrored without
        # --remove so deleted source files stay backed up.
        mc rm --recursive --force --older-than 30d "backup/$BACKUP_S3_BUCKET/postgres/" >/dev/null 2>&1 || true
    '

echo "[$(date -u +%FT%TZ)] whumpf backup $TIMESTAMP complete"
