#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"
if [ ! -f "$TOKEN_FILE" ]; then
    cat /proc/sys/kernel/random/uuid > "$TOKEN_FILE"
    bashio::log.info "Neues MCP-Token erzeugt."
fi
export MCP_TOKEN=$(cat "$TOKEN_FILE")
bashio::log.info "MCP-Token (für MCP-Proxy): ${MCP_TOKEN}"

export SB_ROOT=$(bashio::config 'folder')
export SB_READ_ONLY=$(bashio::config 'read_only')
export SB_MAX_FILE_KB=$(bashio::config 'max_file_kb')
export SB_PORT=8773

mkdir -p "$SB_ROOT"

# Grundstruktur nur anlegen, wenn der Ordner noch leer ist (nie überschreiben)
if bashio::config.true 'create_structure' && [ -z "$(ls -A "$SB_ROOT" 2>/dev/null)" ]; then
    bashio::log.info "Leerer Ordner – lege Grundstruktur an..."
    cp -r /skeleton/. "$SB_ROOT"/
fi

bashio::log.info "Starte MCP Second Brain auf Port 8773, Ordner: ${SB_ROOT}"
exec python3 /server.py
