#!/usr/bin/env bash
set -e

CONFIG_DIR="/data"
SYNC_CONFIG="${CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR="${CONFIG_DIR}/onedrive"
SHARE_DIR="/share/onedrive"

# Create directories
mkdir -p "${SHARE_DIR}"
mkdir -p "${ONEDRIVE_CONFIG_DIR}"
mkdir -p "${CONFIG_DIR}"

# Init sync config if not exists
if [ ! -f "${SYNC_CONFIG}" ]; then
  echo '{}' > "${SYNC_CONFIG}"
fi

# Read sync interval directly from HA options.json (no bashio dependency)
OPTIONS_FILE="/data/options.json"
if [ -f "${OPTIONS_FILE}" ]; then
  SYNC_INTERVAL=$(python3 -c "import json; d=json.load(open('${OPTIONS_FILE}')); print(d.get('sync_interval', 300))")
else
  SYNC_INTERVAL=300
fi

echo "[OneDrive SyncServer] Starting web UI on port 8765..."
python3 /app/server.py &
WEBUI_PID=$!

# Wait for auth token to exist before starting sync
echo "[OneDrive SyncServer] Waiting for OneDrive authentication..."
while [ ! -f "${ONEDRIVE_CONFIG_DIR}/refresh_token" ]; do
  sleep 5
done

echo "[OneDrive SyncServer] Auth token found. Starting sync loop (interval: ${SYNC_INTERVAL}s)..."

# Initial sync: OneDrive -> HA (OneDrive is master)
onedrive \
  --confdir "${ONEDRIVE_CONFIG_DIR}" \
  --synchronize \
  --download-only \
  --verbose 2>&1 | tee -a /data/sync.log

echo "[OneDrive SyncServer] Initial download complete. Starting monitor mode..."

# Sync loop
while true; do
  sleep "${SYNC_INTERVAL}"
  python3 /app/sync_manager.py 2>&1 | tee -a /data/sync.log
done
