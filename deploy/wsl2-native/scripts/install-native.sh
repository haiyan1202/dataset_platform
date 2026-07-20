#!/usr/bin/env bash
set -euo pipefail

[[ ${EUID} -eq 0 ]] || { echo "Run with: sudo ./deploy/wsl2-native/scripts/install-native.sh" >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)
cd "$source_dir"

if [[ $(ps -p 1 -o comm= | tr -d ' ') != systemd ]]; then
  echo "systemd is not enabled. Run enable-systemd.sh, execute wsl --shutdown in Windows, then retry." >&2
  exit 1
fi
[[ -f deploy/wsl2-native/.env ]] || { echo "Missing deploy/wsl2-native/.env" >&2; exit 1; }
[[ -f frontend/dist/index.html ]] || { echo "Missing frontend/dist. Run npm run build first." >&2; exit 1; }

# shellcheck disable=SC1091
set -a
source deploy/wsl2-native/.env
set +a
for value in POSTGRES_PASSWORD MINIO_ROOT_PASSWORD APP_BOOTSTRAP_PASSWORD TOKEN_SECRET; do
  [[ ${!value} != *CHANGE_THIS* ]] || { echo "Replace $value in deploy/wsl2-native/.env" >&2; exit 1; }
done
[[ ${#TOKEN_SECRET} -ge 32 ]] || { echo "TOKEN_SECRET must have at least 32 characters" >&2; exit 1; }
[[ $POSTGRES_USER =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "POSTGRES_USER is invalid" >&2; exit 1; }
[[ $POSTGRES_DB =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || { echo "POSTGRES_DB is invalid" >&2; exit 1; }

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl rsync nginx postgresql redis-server \
  python3 python3-venv python3-dev build-essential libpq-dev

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "/var/lib/$APP_USER" --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_ROOT" "$MINIO_DATA_DIR" "$WORKER_TMP_DIR"
install -d -o root -g "$APP_USER" -m 0750 /etc/dataset-platform
install -m 0640 -o root -g "$APP_USER" deploy/wsl2-native/.env /etc/dataset-platform/dataset-platform.env

if [[ ! -x "$MINIO_BINARY" ]]; then
  if [[ -n "${MINIO_LOCAL_SOURCE:-}" && -f "$MINIO_LOCAL_SOURCE" ]]; then
    echo "Trying cached MinIO binary: $MINIO_LOCAL_SOURCE"
    chmod 0755 "$MINIO_LOCAL_SOURCE"
    if "$MINIO_LOCAL_SOURCE" --version >/dev/null 2>&1; then
      install -m 0755 "$MINIO_LOCAL_SOURCE" "$MINIO_BINARY"
    else
      echo "Cached MinIO binary is incomplete; falling back to resumable download." >&2
    fi
  fi
  if [[ ! -x "$MINIO_BINARY" ]]; then
    minio_download="${MINIO_BINARY}.download"
    echo "Downloading MinIO; this is a large binary and may take several minutes. Re-running this script resumes a partial download."
    curl --fail --location --retry 5 --retry-all-errors --continue-at - --progress-bar \
      --output "$minio_download" https://dl.min.io/server/minio/release/linux-amd64/minio
    chmod 0755 "$minio_download"
    "$minio_download" --version >/dev/null
    mv "$minio_download" "$MINIO_BINARY"
  fi
fi


install -d -m 0755 "$APP_ROOT"
if [[ $(realpath -m "$source_dir") != $(realpath -m "$APP_ROOT") ]]; then
  rsync -a --delete \
    --exclude '.venv' --exclude 'node_modules' --exclude '.pytest_cache' --exclude '.ruff_cache' \
    --exclude '.pytest-tmp' --exclude '__pycache__' --exclude '*.pyc' \
    "$source_dir/" "$APP_ROOT/"
  chown -R root:root "$APP_ROOT"
else
  echo "Using the in-place source tree: $APP_ROOT"
fi
# The system service must be able to traverse a code tree under /home/<user>.
if [[ "$APP_ROOT" == /home/*/*/* ]]; then
  chmod o+x "$(dirname "$(dirname "$APP_ROOT")")"
fi
chown -R "$APP_USER:$APP_USER" "$DATA_ROOT"

python3 -m venv "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/pip" install --upgrade pip
"$APP_ROOT/.venv/bin/pip" install "$APP_ROOT"

systemctl enable --now postgresql redis-server
password_sql=${POSTGRES_PASSWORD//\'/\'\'}
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$POSTGRES_USER'" | grep -q 1; then
  sudo -u postgres createuser --login --no-superuser --no-createdb --no-createrole "$POSTGRES_USER"
fi
sudo -u postgres psql -c "ALTER USER \"$POSTGRES_USER\" PASSWORD '$password_sql'"
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB'" | grep -q 1; then
  sudo -u postgres createdb --owner="$POSTGRES_USER" "$POSTGRES_DB"
fi

install -m 0644 deploy/wsl2-native/systemd/dataset-platform-minio.service /etc/systemd/system/dataset-platform-minio.service
escaped_app_root=$(printf '%s' "$APP_ROOT" | sed 's/[\/&]/\\&/g')
sed "s#@APP_ROOT@#$escaped_app_root#g" deploy/wsl2-native/systemd/dataset-platform-api.service > /etc/systemd/system/dataset-platform-api.service
sed "s#@APP_ROOT@#$escaped_app_root#g" deploy/wsl2-native/systemd/dataset-platform-worker.service > /etc/systemd/system/dataset-platform-worker.service
sed "s#@APP_ROOT@#$escaped_app_root#g" deploy/wsl2-native/nginx/dataset-platform.conf > /etc/nginx/sites-available/dataset-platform
ln -sfn /etc/nginx/sites-available/dataset-platform /etc/nginx/sites-enabled/dataset-platform
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl daemon-reload
systemctl enable --now dataset-platform-minio

runuser -u "$APP_USER" -- bash -lc "set -a; source /etc/dataset-platform/dataset-platform.env; set +a; cd '$APP_ROOT'; export PYTHONPATH='$APP_ROOT/src:$APP_ROOT/backend'; '$APP_ROOT/.venv/bin/alembic' -c backend/alembic.ini upgrade head; '$APP_ROOT/.venv/bin/python' -m app.bootstrap"
systemctl enable --now dataset-platform-api dataset-platform-worker nginx
systemctl restart dataset-platform-api dataset-platform-worker nginx

printf '%s\n' "Native deployment installed. Verify with: curl http://127.0.0.1:${NGINX_PORT}/health/ready"


