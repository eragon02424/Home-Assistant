#!/usr/bin/env bash
set -e

CONFIG_DIR="/data"
SYNC_CONFIG="${CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR="${CONFIG_DIR}/onedrive"
SHARE_DIR="/share/onedrive"

mkdir -p "${SHARE_DIR}" "${ONEDRIVE_CONFIG_DIR}" "${CONFIG_DIR}"

if [ ! -f "${SYNC_CONFIG}" ]; then
  echo '{}' > "${SYNC_CONFIG}"
fi

OPTIONS_FILE="/data/options.json"
if [ -f "${OPTIONS_FILE}" ]; then
  SYNC_INTERVAL=$(python3 -c "import json; d=json.load(open('${OPTIONS_FILE}')); print(d.get('sync_interval', 300))")
else
  SYNC_INTERVAL=300
fi

echo "[OneDrive SyncServer] Starting Ingress UI on port 8772..."
python3 /app/server.py 8772 &

echo "[OneDrive SyncServer] Starting direct auth UI on port 8771..."
python3 /app/server.py 8771 &

echo "[OneDrive SyncServer] Waiting for OneDrive authentication..."
while [ ! -f "${ONEDRIVE_CONFIG_DIR}/refresh_token" ]; do
  sleep 5
done

echo "[OneDrive SyncServer] Auth token found. Starting sync (interval: ${SYNC_INTERVAL}s)..."

onedrive \
  --confdir "${ONEDRIVE_CONFIG_DIR}" \
  --synchronize \
  --download-only \
  --verbose 2>&1 | tee -a /data/sync.log

echo "[OneDrive SyncServer] Initial sync complete."

while true; do
  sleep "${SYNC_INTERVAL}"
  python3 /app/sync_manager.py 2>&1 | tee -a /data/sync.log
done
