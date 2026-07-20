#!/usr/bin/env bash
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
config_file=/etc/dataset-platform/dataset-platform.env
if [[ ! -r "$config_file" ]]; then
  config_file="$repo_root/deploy/wsl2-native/.env"
fi
# shellcheck disable=SC1090
set -a
source "$config_file"
set +a

systemctl --no-pager --full status dataset-platform-minio dataset-platform-api dataset-platform-worker nginx || true
ready_json=$(curl --fail --silent --show-error "http://127.0.0.1:${NGINX_PORT}/health/ready")
python3 - "$ready_json" <<'PY'
import json
import sys

payload = json.loads(sys.argv[1])
if payload.get("status") != "ok":
    raise SystemExit(f"Readiness failed: {json.dumps(payload, sort_keys=True)}")
print(json.dumps(payload, sort_keys=True))
PY
