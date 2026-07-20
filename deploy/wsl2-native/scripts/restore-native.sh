#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "Run with: sudo ./deploy/wsl2-native/scripts/restore-native.sh BACKUP_DIRECTORY" >&2; exit 1; }
source_dir=${1:?"Usage: restore-native.sh BACKUP_DIRECTORY"}
config_file=/etc/dataset-platform/dataset-platform.env
[[ -r "$config_file" ]] || { echo "Missing $config_file" >&2; exit 1; }
[[ -s "$source_dir/postgres.sql" ]] || { echo "Missing $source_dir/postgres.sql" >&2; exit 1; }
[[ -d "$source_dir/minio" ]] || { echo "Missing $source_dir/minio" >&2; exit 1; }
set -a
source "$config_file"
set +a
if [[ -f "$source_dir/SHA256SUMS" ]]; then
  (cd "$source_dir" && sha256sum --check SHA256SUMS)
fi

printf '%s\n' "WARNING: this replaces the native database and object bucket with $source_dir."
printf '%s' "Type RESTORE to continue: "
read -r answer
[[ "$answer" == RESTORE ]] || { echo "Cancelled."; exit 0; }

systemctl stop dataset-platform-api dataset-platform-worker
runuser -u postgres -- psql -d postgres -v ON_ERROR_STOP=1 -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$POSTGRES_DB' AND pid <> pg_backend_pid();"
runuser -u postgres -- dropdb --if-exists "$POSTGRES_DB"
runuser -u postgres -- createdb --owner="$POSTGRES_USER" "$POSTGRES_DB"
export PGPASSWORD="$POSTGRES_PASSWORD"
psql --host=127.0.0.1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" -v ON_ERROR_STOP=1 < "$source_dir/postgres.sql"
"$APP_ROOT/.venv/bin/python" - "$source_dir/minio" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

from minio import Minio

source = Path(sys.argv[1])
client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ROOT_USER"],
    secret_key=os.environ["MINIO_ROOT_PASSWORD"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)
bucket = os.environ["MINIO_BUCKET"]
if not client.bucket_exists(bucket):
    client.make_bucket(bucket)
for item in client.list_objects(bucket, recursive=True):
    client.remove_object(bucket, item.object_name)
for path in source.rglob("*"):
    if not path.is_file():
        continue
    relative = path.relative_to(source)
    key = PurePosixPath(relative.as_posix())
    if key.is_absolute() or ".." in key.parts:
        raise RuntimeError(f"Unsafe backup path: {relative}")
    client.fput_object(bucket, key.as_posix(), str(path))
PY
systemctl start dataset-platform-api dataset-platform-worker
printf '%s\n' "Native restore completed. Verify with ./deploy/wsl2-native/scripts/status-native.sh"