#!/usr/bin/env bash
# Nightly PostgreSQL backup. Add to cron:  15 3 * * *  cd /opt/falaron && ./scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p backups
stamp="$(date +%Y%m%d-%H%M%S)"
docker compose exec -T db pg_dump -U falaron -d falaron --no-owner | gzip > "backups/falaron-${stamp}.sql.gz"
# keep 14 days
find backups -name 'falaron-*.sql.gz' -mtime +14 -delete
echo "backup written: backups/falaron-${stamp}.sql.gz"
