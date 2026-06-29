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

bashio::log.info "Starting MCP Shell v1.0.0 on port 8766..."

exec python3 /server.py
