#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"
[ -f deploy/wsl2/.env ] || { echo "Missing deploy/wsl2/.env" >&2; exit 1; }
docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml down
