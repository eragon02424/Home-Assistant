#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"
if [ ! -f "$TOKEN_FILE" ]; then
    cat /proc/sys/kernel/random/uuid > "$TOKEN_FILE"
    bashio::log.info "Neues MCP-Token erzeugt."
fi
export MCP_TOKEN=$(cat "$TOKEN_FILE")
bashio::log.info "MCP-Token (für MCP-Proxy): ${MCP_TOKEN}"

export SB_ROOT=$(bashio::config 'folder')
export SB_BRAINS=$(bashio::config 'brains')
export SB_READ_ONLY=$(bashio::config 'read_only')
export SB_MAX_FILE_KB=$(bashio::config 'max_file_kb')
export SB_MAX_UPLOAD_MB=$(bashio::config 'max_upload_mb')
if bashio::config.has_value 'import_dir'; then
    export SB_IMPORT_DIR=$(bashio::config 'import_dir')
fi
export SB_PORT=8773

mkdir -p "$SB_ROOT"

# Ordnerstruktur je Brain legt server.py selbst an (nur fehlende Ordner, nie Dateien).
bashio::log.info "Starte MCP Second Brain auf Port 8773, Ordner: ${SB_ROOT}, Brains: ${SB_BRAINS}"
exec python3 /server.py
