#!/usr/bin/env bash
# scripts/backup.sh
# ══════════════════════════════════════════════════════════════════════════════
# HandyDoctor — PostgreSQL database backup
# ══════════════════════════════════════════════════════════════════════════════
#
# Creates a compressed pg_dump of the handydoctor database, rotates old local
# backups (keeping 7 days), and optionally uploads to S3 / compatible storage.
#
# Schedule with cron (example — daily at 03:00):
#   0 3 * * * /opt/handydoctor/scripts/backup.sh >> /var/log/handydoctor-backup.log 2>&1
#
# Or add as a Docker service / cronjob in docker-compose.prod.yml.
#
# Required environment variables (sourced from .env.production or set directly):
#   POSTGRES_USER         — database user
#   POSTGRES_PASSWORD     — database password
#   POSTGRES_DB           — database name
#   POSTGRES_HOST         — hostname (default: localhost)
#   POSTGRES_PORT         — port (default: 5432)
#
# Optional:
#   BACKUP_DIR            — local backup directory (default: /opt/handydoctor/backups)
#   BACKUP_RETENTION_DAYS — how many days to keep local backups (default: 7)
#   S3_BUCKET             — if set, upload to s3://BUCKET/handydoctor/FILENAME
#   S3_ENDPOINT           — custom S3 endpoint URL (for MinIO, Backblaze B2, etc.)
#
# Dependencies: pg_dump, gzip, aws CLI (optional for S3 upload)
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Config (with sensible defaults) ──────────────────────────────────────────
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-hduser}"
POSTGRES_DB="${POSTGRES_DB:-handydoctor}"
BACKUP_DIR="${BACKUP_DIR:-/opt/handydoctor/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="${BACKUP_DIR}/handydoctor_${TIMESTAMP}.sql.gz"

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [backup] $*"; }
ok()  { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [backup] ✅ $*"; }
err() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') [backup] ❌ $*" >&2; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v pg_dump &>/dev/null || err "pg_dump not found. Install postgresql-client."
command -v gzip    &>/dev/null || err "gzip not found."

[[ -n "${POSTGRES_PASSWORD:-}" ]] || err "POSTGRES_PASSWORD is not set."

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ── Dump ─────────────────────────────────────────────────────────────────────
log "Starting backup of database '${POSTGRES_DB}' on ${POSTGRES_HOST}:${POSTGRES_PORT}..."

PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    --host="$POSTGRES_HOST" \
    --port="$POSTGRES_PORT" \
    --username="$POSTGRES_USER" \
    --format=plain \
    --no-owner \
    --no-acl \
    "$POSTGRES_DB" \
    | gzip -9 \
    > "$BACKUP_FILE"

BACKUP_SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
ok "Backup written to ${BACKUP_FILE} (${BACKUP_SIZE})"

# ── S3 upload (optional) ──────────────────────────────────────────────────────
if [[ -n "${S3_BUCKET:-}" ]]; then
    if ! command -v aws &>/dev/null; then
        echo "WARNING: S3_BUCKET set but 'aws' CLI not found — skipping upload"
    else
        log "Uploading to s3://${S3_BUCKET}/handydoctor/$(basename "$BACKUP_FILE")..."

        S3_CMD=(aws s3 cp "$BACKUP_FILE" "s3://${S3_BUCKET}/handydoctor/$(basename "$BACKUP_FILE")")

        if [[ -n "${S3_ENDPOINT:-}" ]]; then
            S3_CMD+=(--endpoint-url "$S3_ENDPOINT")
        fi

        "${S3_CMD[@]}" --quiet
        ok "Uploaded to S3 successfully"
    fi
fi

# ── Retention: remove backups older than BACKUP_RETENTION_DAYS ───────────────
log "Pruning backups older than ${BACKUP_RETENTION_DAYS} days..."
OLD_COUNT=0
while IFS= read -r old_file; do
    rm -f "$old_file"
    OLD_COUNT=$((OLD_COUNT + 1))
done < <(find "$BACKUP_DIR" -name "handydoctor_*.sql.gz" -mtime "+${BACKUP_RETENTION_DAYS}" 2>/dev/null)

if [[ $OLD_COUNT -gt 0 ]]; then
    log "Removed ${OLD_COUNT} old backup(s)"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
REMAINING=$(find "$BACKUP_DIR" -name "handydoctor_*.sql.gz" | wc -l)
ok "Backup complete. ${REMAINING} backup(s) retained in ${BACKUP_DIR}"

# ── Restore instructions ──────────────────────────────────────────────────────
cat << 'RESTORE'

# To restore this backup:
#   gunzip -c /path/to/handydoctor_TIMESTAMP.sql.gz \
#     | PGPASSWORD=YOUR_PASSWORD psql \
#         --host=HOST --port=5432 --username=hduser handydoctor
#
# In Docker:
#   gunzip -c BACKUP_FILE | docker exec -i handydoctor-postgres-1 \
#     psql -U hduser handydoctor

RESTORE
