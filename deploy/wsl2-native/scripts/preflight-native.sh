#!/usr/bin/env sh
set -eu

fail() { printf '%s\n' "ERROR: $*" >&2; exit 1; }
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$repo_root"

[ "$(uname -s)" = "Linux" ] || fail "Run this script inside WSL Linux."
[ "$(ps -p 1 -o comm= | tr -d ' ')" = "systemd" ] || fail "systemd is not enabled. Run sudo ./deploy/wsl2-native/scripts/enable-systemd.sh, then run wsl --shutdown from Windows."
command -v sudo >/dev/null 2>&1 || fail "sudo is required for package and service installation."
[ -f deploy/wsl2-native/.env ] || fail "Missing deploy/wsl2-native/.env. Copy env.example and replace all secrets."
[ -f frontend/dist/index.html ] || fail "frontend/dist is missing. Build the frontend with npm run build before native installation."
python3 -c "import ensurepip" >/dev/null 2>&1 || fail "python3-venv is unavailable. The native installer will install it; otherwise run sudo apt-get install -y python3-venv."

. deploy/wsl2-native/.env
case "$POSTGRES_USER" in ''|*[!A-Za-z0-9_]*) fail "POSTGRES_USER must contain only letters, digits, and underscores." ;; esac
case "$POSTGRES_DB" in ''|*[!A-Za-z0-9_]*) fail "POSTGRES_DB must contain only letters, digits, and underscores." ;; esac
case "$POSTGRES_PASSWORD$MINIO_ROOT_PASSWORD$APP_BOOTSTRAP_PASSWORD$TOKEN_SECRET" in *CHANGE_THIS*) fail "Replace every CHANGE_THIS placeholder in deploy/wsl2-native/.env." ;; esac
[ "${#TOKEN_SECRET}" -ge 32 ] || fail "TOKEN_SECRET must contain at least 32 characters."

disk_kb=$(df -Pk . | awk 'NR == 2 {print $4}')
[ "${disk_kb:-0}" -ge 10485760 ] || fail "At least 10 GiB free disk is required; currently ${disk_kb:-0} KiB."
case "$repo_root" in /mnt/*) printf '%s\n' "WARNING: source is under /mnt; install-native.sh will copy it to /opt for runtime performance." >&2 ;; esac
printf '%s\n' "Native WSL preflight passed."

