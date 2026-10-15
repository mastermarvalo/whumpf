#!/bin/bash
# Whumpf backup — daily local dump plus a snapshot to any S3-compatible bucket.
#
# Local: `make db-dump` (pg_dump -Fc, verified) into $BACKUP_DIR (default
# /data/backups); dumps older than 14 days are deleted. Runs even when the
# off-site bucket is not configured.
#
# Off-site:
#   * That same Postgres dump (postgres/whumpf-<utc>.dump), read back and
#     checked by sha256 after upload
#   * MinIO `user-uploads` bucket (GPX, photos)
#   * Optionally `dem-cogs` (set BACKUP_INCLUDE_COGS=true; multi-GB)
#
# Skips:
#   * `dem-raw` — redownloadable from USGS 3DEP, no point paying to store it
#   * `strava-cache` — regenerable
#
# Retention: off-site postgres dumps older than 30 days are pruned; user-uploads is
# mirrored without --remove so deletions on the source don't wipe the backup.
#
# Scheduled via cron, 3am server time daily (docs/external-services.md):
#   0 3 * * * cd /home/ronkerflonk/whumpf && flock -n /data/backups/.backup.lock scripts/backup.sh >> /data/backups/backup.log 2>&1
#
# Configure: set BACKUP_S3_* in .env. See docs/external-services.md.

set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck source=/dev/null
set -a; . ./.env; set +a

# MinIO stopped serving mc images anonymously (quay.io and Docker Hub), so this
# release only exists where it is already cached. Keep compose.yml in step.
MC_IMAGE=quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z
BACKUP_DIR=${BACKUP_DIR:-/data/backups}
LOCAL_RETENTION_DAYS=14

# ── local dump ────────────────────────────────────────────────────────────────
# `make db-dump` writes inside the postgis container, checks the dump with
# pg_restore --list, and verifies the copy out by sha256. The same file is
# uploaded below. Never stream pg_dump through `podman exec`: podman cuts the
# stream short and still exits 0 (two such truncated uploads on 2026-10-14
# were reported "complete").
# A failure here must not stop mirroring user-uploads; it fails the run.
DUMP_NAME="whumpf-$(date -u +%Y%m%dT%H%M%SZ).dump"
LOCAL_OK=1
echo "[$(date -u +%FT%TZ)] local db-dump to $BACKUP_DIR/$DUMP_NAME"
if make --no-print-directory db-dump BACKUP_DIR="$BACKUP_DIR" DUMP_NAME="$DUMP_NAME"; then
    DUMP_SHA=$(sha256sum "$BACKUP_DIR/$DUMP_NAME" | cut -d' ' -f1)
    # -mtime +N is "more than N whole days old", so this keeps 14 days.
    find "$BACKUP_DIR" -maxdepth 1 -name 'whumpf-*.dump' \
        -mtime +$((LOCAL_RETENTION_DAYS - 1)) -print -delete | sed 's/^/    pruned /'
else
    LOCAL_OK=0
    DUMP_SHA=""
    echo "  !! local db-dump FAILED"
fi

if [ -z "${BACKUP_S3_ENDPOINT:-}" ] || [ -z "${BACKUP_S3_BUCKET:-}" ] \
   || [ -z "${BACKUP_S3_ACCESS_KEY:-}" ] || [ -z "${BACKUP_S3_SECRET_KEY:-}" ]; then
    echo "BACKUP_S3_* env vars not configured — no off-site copy."
    echo "See docs/external-services.md → Backups."
    [ "$LOCAL_OK" = 1 ] || exit 1
    exit 0
fi

echo "[$(date -u +%FT%TZ)] whumpf backup $DUMP_NAME starting"

# ── upload via mc (one-shot container, no host install) ───────────────────────
MIRROR_COGS=""
if [ "${BACKUP_INCLUDE_COGS:-false}" = "true" ]; then
    MIRROR_COGS="mc mirror --overwrite local/dem-cogs backup/$BACKUP_S3_BUCKET/dem-cogs/ &&"
fi
# Without a good local dump there is nothing to upload for postgres; still
# mirror user-uploads. An empty dir mount keeps the podman command the same.
if [ "$LOCAL_OK" = 1 ]; then
    DUMP_MOUNT="$BACKUP_DIR/$DUMP_NAME:/work/$DUMP_NAME:ro"
else
    EMPTY=$(mktemp -d); trap 'rmdir "$EMPTY"' EXIT
    DUMP_MOUNT="$EMPTY:/work:ro"
fi

echo "  → uploading to $BACKUP_S3_ENDPOINT/$BACKUP_S3_BUCKET"
export DUMP_NAME DUMP_SHA
podman run --rm --network host \
    -e BACKUP_S3_ENDPOINT \
    -e BACKUP_S3_BUCKET \
    -e BACKUP_S3_ACCESS_KEY \
    -e BACKUP_S3_SECRET_KEY \
    -e MINIO_ROOT_USER \
    -e MINIO_ROOT_PASSWORD \
    -e DUMP_NAME \
    -e DUMP_SHA \
    -v "$DUMP_MOUNT" \
    --entrypoint sh "$MC_IMAGE" -c '
        set -e
        mc alias set local  http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
        mc alias set backup "$BACKUP_S3_ENDPOINT" "$BACKUP_S3_ACCESS_KEY" "$BACKUP_S3_SECRET_KEY" >/dev/null
        if [ -n "$DUMP_SHA" ]; then
            dest="backup/$BACKUP_S3_BUCKET/postgres/$DUMP_NAME"
            mc cp "/work/$DUMP_NAME" "$dest" >/dev/null
            # Read it back: only a byte-identical copy counts as backed up.
            got=$(mc cat "$dest" | sha256sum | cut -d" " -f1)
            if [ "$got" != "$DUMP_SHA" ]; then
                echo "  !! uploaded dump does not match the local one ($got != $DUMP_SHA)"
                exit 1
            fi
            echo "    uploaded and verified postgres/$DUMP_NAME (sha256 $got)"
        fi
        mc mirror --overwrite local/user-uploads "backup/$BACKUP_S3_BUCKET/user-uploads/" >/dev/null
        '"$MIRROR_COGS"'
        # 30-day retention on postgres dumps. user-uploads is mirrored without
        # --remove so deleted source files stay backed up.
        mc rm --recursive --force --older-than 30d "backup/$BACKUP_S3_BUCKET/postgres/" >/dev/null 2>&1 || true
    '

if [ "$LOCAL_OK" != 1 ]; then
    echo "[$(date -u +%FT%TZ)] whumpf backup $DUMP_NAME: user-uploads mirrored, POSTGRES DUMP FAILED"
    exit 1
fi
echo "[$(date -u +%FT%TZ)] whumpf backup $DUMP_NAME complete"
