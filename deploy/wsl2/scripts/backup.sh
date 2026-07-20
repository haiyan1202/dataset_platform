#!/usr/bin/env sh
set -eu

out_dir=${1:?"Usage: backup.sh OUTPUT_DIRECTORY"}
stamp=$(date +%Y%m%d_%H%M%S)
target="$out_dir/$stamp"
mkdir -p "$target/minio"

compose="docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml"
pg_user=$(grep '^POSTGRES_USER=' deploy/wsl2/.env | cut -d= -f2-)
pg_database=$(grep '^POSTGRES_DB=' deploy/wsl2/.env | cut -d= -f2-)
$compose exec -T postgres pg_dump -U "$pg_user" "$pg_database" > "$target/postgres.sql"

# `compose run` joins the private network and mounts only this explicit backup directory.
backup_abs=$(cd "$target" && pwd)
$compose run --rm --no-deps -v "$backup_abs:/backup" --entrypoint /bin/sh minio-init -c \
  'mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc mirror --overwrite local/"$MINIO_BUCKET" /backup/minio'

( cd "$target" && sha256sum postgres.sql > SHA256SUMS )
printf '%s\n' "Complete PostgreSQL + MinIO backup written to $target"
