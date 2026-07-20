#!/usr/bin/env sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"
./deploy/wsl2/scripts/preflight.sh

compose="docker compose --env-file deploy/wsl2/.env -f deploy/wsl2/compose.yaml"
$compose up --build -d
$compose exec api alembic upgrade head
$compose exec api python -m app.bootstrap
$compose ps
printf '%s\n' "Platform started. Verify: curl http://127.0.0.1:8080/health/ready"

