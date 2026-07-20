#!/usr/bin/env sh
set -eu

source_dir=${1:?"Usage: restore.sh BACKUP_DIRECTORY"}
sql_file="$source_dir/postgres.sql"
object_dir="$source_dir/minio"
[ -s "$sql_file" ] || { echo "Missing or empty $sql_file" >&2; exit 1; }
[ -d "$object_dir" ] || { echo "Missing object snapshot $object_dir" >&2; exit 1; }

compose="docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml"
printf '%s\n' "WARNING: this overwrites database rows and bucket objects. Stop api/worker before continuing."
printf '%s\n' "Type RESTORE to continue:"
read answer
[ "$answer" = "RESTORE" ] || { echo "Cancelled."; exit 1; }

pg_user=$(grep '^POSTGRES_USER=' deploy/wsl2/.env | cut -d= -f2-)
pg_database=$(grep '^POSTGRES_DB=' deploy/wsl2/.env | cut -d= -f2-)
$compose exec -T postgres psql -U "$pg_user" -d "$pg_database" < "$sql_file"
restore_abs=$(cd "$object_dir" && pwd)
$compose run --rm --no-deps -v "$restore_abs:/restore:ro" --entrypoint /bin/sh minio-init -c \
  'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc mirror --overwrite --remove /restore local/"$MINIO_BUCKET"'
printf '%s\n' "Restore completed. Restart API and worker, then verify /health/ready."

