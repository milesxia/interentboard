#!/bin/sh
set -eu
cd /share/Container/internetboard 2>/dev/null || cd "$(dirname "$0")"
TS=$(date +%Y%m%d-%H%M%S)
OUT="backups/internetboard-${TS}"
mkdir -p "$OUT"
. ./.env

echo "Exporting PostgreSQL..."
docker exec internetboard-postgres pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc > "$OUT/database.dump"
echo "Archiving evidence/knowledge..."
tar -czf "$OUT/data.tgz" data
echo "Saving configuration..."
cp .env docker-compose.yml "$OUT/"
tar -czf "${OUT}.tgz" -C backups "$(basename "$OUT")"
rm -rf "$OUT"
echo "Backup created: /share/Container/internetboard/${OUT}.tgz"
