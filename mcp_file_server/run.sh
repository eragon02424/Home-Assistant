#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"

if [ ! -f "$TOKEN_FILE" ]; then
    NEW_TOKEN=$(cat /proc/sys/kernel/random/uuid)
    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    bashio::log.info "Generated new MCP token: ${NEW_TOKEN}"
else
    bashio::log.info "Using existing MCP token: $(cat $TOKEN_FILE)"
fi

export MCP_TOKEN=$(cat "$TOKEN_FILE")
export MCP_ALLOWED_PATHS=$(bashio::config 'allowed_paths' | tr '\n' ',')

bashio::log.info "Starting MCP File Server v1.3.0 on port 8765..."
bashio::log.info "Allowed paths: ${MCP_ALLOWED_PATHS}"

exec python3 /server.py
