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
export SYNC_API_URL=$(bashio::config 'syncserver_api_url')
export ENDPOINT_TOKEN=$(bashio::config 'endpoint_token')

if [ -z "$ENDPOINT_TOKEN" ] || [ "$ENDPOINT_TOKEN" = "null" ]; then
    bashio::log.warning "endpoint_token ist nicht gesetzt - Aufrufe an den SyncServer schlagen fehl."
    export ENDPOINT_TOKEN=""
fi

bashio::log.info "Starting MCP OneDrive v0.1.0 on port 8775..."
bashio::log.info "OneDrive SyncServer API: ${SYNC_API_URL}"

exec python3 /server.py
