#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { echo "Usage: $0 /path/to/internetboard-YYYYMMDD-HHMMSS.tgz"; exit 2; }
BACKUP="$1"
[ -f "$BACKUP" ] || { echo "Backup not found: $BACKUP"; exit 2; }
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")"
. ./.env
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

tar -xzf "$BACKUP" -C "$TMP"
DIR=$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -n1)
[ -n "$DIR" ] || { echo "Invalid backup"; exit 2; }

if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"; else COMPOSE="docker-compose"; fi
$COMPOSE stop backend worker scheduler frontend
rm -rf data.restore
tar -xzf "$DIR/data.tgz" -C "$TMP"
mv data data.restore 2>/dev/null || true
mv "$TMP/data" data

docker exec internetboard-postgres dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker exec internetboard-postgres createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
cat "$DIR/database.dump" | docker exec -i internetboard-postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges
$COMPOSE up -d backend worker scheduler frontend
echo "Restore complete. Previous data directory, if any, is data.restore"
