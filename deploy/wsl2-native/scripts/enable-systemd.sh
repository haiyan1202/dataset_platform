#!/usr/bin/env sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "Run with: sudo ./deploy/wsl2-native/scripts/enable-systemd.sh" >&2; exit 1; }

if [ -f /etc/wsl.conf ] && grep -q '^systemd=true$' /etc/wsl.conf; then
  echo "WSL systemd is already enabled."
  exit 0
fi

cat >> /etc/wsl.conf <<'EOF'

[boot]
systemd=true
EOF
printf '%s\n' "systemd=true was written to /etc/wsl.conf."
printf '%s\n' "From Windows PowerShell run: wsl --shutdown"
printf '%s\n' "Then reopen Ubuntu-22.04 and run: ps -p 1 -o comm="
