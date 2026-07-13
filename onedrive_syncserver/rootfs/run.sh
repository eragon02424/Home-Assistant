#!/usr/bin/env bash
set -e

CONFIG_DIR="/data"
SYNC_CONFIG="${CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR="${CONFIG_DIR}/onedrive"
SHARE_DIR="/share/onedrive"
LOCK_FILE="${CONFIG_DIR}/sync.lock"

mkdir -p "${SHARE_DIR}" "${ONEDRIVE_CONFIG_DIR}" "${CONFIG_DIR}"

if [ ! -f "${SYNC_CONFIG}" ]; then
  echo '{}' > "${SYNC_CONFIG}"
fi

# Datei-/Ordnerrechte fuer synchronisierte Dateien: der onedrive-Client
# verwendet standardmaessig sync_file_permissions=600 / sync_dir_permissions=700
# (nur root lesbar). Andere Add-ons (z.B. Paperless-ngx), die im selben
# /share-Ordner lesen muessen, kommen damit nicht an die Dateien.
# Deshalb hier beim ersten Start auf 644/755 setzen - idempotent, ueber-
# schreibt keine ggf. bereits manuell gesetzten Werte in der Config.
ONEDRIVE_CONFIG_FILE="${ONEDRIVE_CONFIG_DIR}/config"
if [ ! -f "${ONEDRIVE_CONFIG_FILE}" ]; then
  echo 'sync_dir = "/share/onedrive"' > "${ONEDRIVE_CONFIG_FILE}"
fi
if ! grep -q '^sync_file_permissions' "${ONEDRIVE_CONFIG_FILE}"; then
  echo 'sync_file_permissions = "644"' >> "${ONEDRIVE_CONFIG_FILE}"
fi
if ! grep -q '^sync_dir_permissions' "${ONEDRIVE_CONFIG_FILE}"; then
  echo 'sync_dir_permissions = "755"' >> "${ONEDRIVE_CONFIG_FILE}"
fi

# Bei Container-Start ist garantiert kein Sync aktiv - ein evtl.
# vorhandener Lock ist verwaist (z.B. weil der Container waehrend eines
# laufenden Syncs neu gestartet wurde, etwa durch ein Add-on-Update).
if [ -f "${LOCK_FILE}" ]; then
  echo "[OneDrive SyncServer] Entferne verwaisten Lock von vorherigem Lauf..."
  rm -f "${LOCK_FILE}"
fi

OPTIONS_FILE="/data/options.json"
if [ -f "${OPTIONS_FILE}" ]; then
  SYNC_INTERVAL=$(python3 -c "import json; d=json.load(open('${OPTIONS_FILE}')); print(d.get('sync_interval', 300))")
else
  SYNC_INTERVAL=300
fi

echo "[OneDrive SyncServer] Starting Ingress UI on port 8772..."
python3 /app/server.py 8772 &

echo "[OneDrive SyncServer] Waiting for OneDrive authentication..."
while [ ! -f "${ONEDRIVE_CONFIG_DIR}/refresh_token" ]; do
  sleep 5
done

echo "[OneDrive SyncServer] Auth token found. Running initial sync via sync_manager.py..."

# WICHTIG: Der initiale Sync laeuft ueber sync_manager.py (nicht direkt
# ueber 'onedrive'), damit die sync_list-Logik (Ordner-Auswahl) auch
# direkt nach einem Container-Neustart greift, statt kurzzeitig alles
# ungefiltert herunterzuladen bevor der periodische Timer das erste Mal
# sync_manager.py aufruft.
python3 /app/sync_manager.py 2>&1 | tee -a /data/sync.log

echo "[OneDrive SyncServer] Initial sync complete."

while true; do
  sleep "${SYNC_INTERVAL}"
  python3 /app/sync_manager.py 2>&1 | tee -a /data/sync.log
done
