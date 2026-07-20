#!/usr/bin/env sh
set -eu

fail() { printf '%s\n' "ERROR: $*" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail "Docker is not installed in this WSL distribution. Install Docker Engine or enable Docker Desktop WSL Integration."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable."
[ -f deploy/wsl2/.env ] || fail "Missing deploy/wsl2/.env. Copy env.example and replace every secret."

available_kb=$(df -Pk . | awk 'NR == 2 {print $4}')
[ "${available_kb:-0}" -ge 10485760 ] || fail "At least 10 GiB free disk is recommended; current free KiB: ${available_kb:-0}."
if command -v free >/dev/null 2>&1; then
  memory_kb=$(free -k | awk '/Mem:/ {print $2}')
  [ "${memory_kb:-0}" -ge 8388608 ] || printf '%s\n' "WARNING: less than 8 GiB memory reported by WSL." >&2
fi
case "$(pwd)" in
  /mnt/*) printf '%s\n' "WARNING: repository is under /mnt; move it to the WSL Linux filesystem for faster large imports." >&2 ;;
esac
printf '%s\n' "WSL2 deployment preflight passed."
