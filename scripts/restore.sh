#!/usr/bin/env bash
# Restore a backup into the running database. DESTRUCTIVE: replaces current data.
#   ./scripts/restore.sh backups/falaron-YYYYMMDD-HHMMSS.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
file="${1:?usage: restore.sh <backup.sql.gz>}"
read -r -p "This REPLACES the current database with ${file}. Type 'restore' to continue: " ok
[ "$ok" = "restore" ] || { echo "aborted"; exit 1; }
docker compose stop app
docker compose exec -T db psql -U falaron -d postgres -c "DROP DATABASE IF EXISTS falaron WITH (FORCE);" -c "CREATE DATABASE falaron OWNER falaron;"
gunzip -c "$file" | docker compose exec -T db psql -U falaron -d falaron
docker compose start app
echo "restored from ${file}"
