#!/usr/bin/env sh
set -eu

sudo systemctl stop dataset-platform-worker dataset-platform-api dataset-platform-minio nginx
printf '%s\n' "Native Dataset Platform services stopped. PostgreSQL and Redis remain installed but can be stopped separately if desired."
