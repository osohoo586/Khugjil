#!/bin/sh
# Nightly encrypted backup (run from the host's cron). The dump is encrypted with age to a public
# key; keep the private key offline. Backups older than the transaction retention are deleted so
# purged transactions also age out of backups (PLAN Q32). Secrets are not part of the dump.
set -eu
: "${AGE_RECIPIENT:?set AGE_RECIPIENT to the age public key (age1...)}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/khugjil}"
RETENTION_DAYS="${RETENTION_DAYS:-90}"
cd "$(dirname "$0")"
mkdir -p "$BACKUP_DIR"
umask 077
out="$BACKUP_DIR/khugjil-$(date -u +%Y%m%dT%H%M%SZ).sql.age"
docker compose exec -T postgres pg_dump -U postgres --no-owner khugjil | age -r "$AGE_RECIPIENT" > "$out"
find "$BACKUP_DIR" -name 'khugjil-*.sql.age' -mtime +"$RETENTION_DAYS" -delete
echo "backup written: $out"
