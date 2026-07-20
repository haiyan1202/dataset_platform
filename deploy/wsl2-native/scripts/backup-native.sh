#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "Run with: sudo ./deploy/wsl2-native/scripts/backup-native.sh OUTPUT_DIRECTORY" >&2; exit 1; }
output_dir=${1:?"Usage: backup-native.sh OUTPUT_DIRECTORY"}
config_file=/etc/dataset-platform/dataset-platform.env
[[ -r "$config_file" ]] || { echo "Missing $config_file" >&2; exit 1; }
set -a
source "$config_file"
set +a

stamp=$(date +%Y%m%d_%H%M%S)
target="$output_dir/$stamp"
install -d -m 0750 "$target/minio"
export PGPASSWORD="$POSTGRES_PASSWORD"
pg_dump --host=127.0.0.1 --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-owner --no-privileges > "$target/postgres.sql"
"$APP_ROOT/.venv/bin/python" - "$target/minio" <<'PY'
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

from minio import Minio

output = Path(sys.argv[1])
client = Minio(
    os.environ["MINIO_ENDPOINT"],
    access_key=os.environ["MINIO_ROOT_USER"],
    secret_key=os.environ["MINIO_ROOT_PASSWORD"],
    secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
)
bucket = os.environ["MINIO_BUCKET"]
if client.bucket_exists(bucket):
    for item in client.list_objects(bucket, recursive=True):
        relative = PurePosixPath(item.object_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"Unsafe object name: {item.object_name}")
        destination = output.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        client.fget_object(bucket, item.object_name, str(destination))
PY
(
  cd "$target"
  sha256sum postgres.sql > SHA256SUMS
  find minio -type f -print0 | sort -z | xargs -0r sha256sum >> SHA256SUMS
)
printf '%s\n' "Native PostgreSQL + MinIO backup written to $target"