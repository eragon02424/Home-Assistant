#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"
KEY_DIR="/data/ssh_key"
KEY_FILE="$KEY_DIR/mcp_shell_key"

if [ ! -f "$TOKEN_FILE" ]; then
    NEW_TOKEN=$(cat /proc/sys/kernel/random/uuid)
    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    bashio::log.info "Generated new MCP token: ${NEW_TOKEN}"
else
    bashio::log.info "Using existing MCP token: $(cat $TOKEN_FILE)"
fi

if [ ! -f "$KEY_FILE" ]; then
    bashio::log.error "SSH private key not found at $KEY_FILE — copy it there before starting."
    bashio::log.error "execute_command will fail until the key is in place."
else
    chmod 600 "$KEY_FILE"
fi

export MCP_TOKEN=$(cat "$TOKEN_FILE")
export SSH_HOST="${SSH_HOST:-127.0.0.1}"
export SSH_PORT="${SSH_PORT:-22}"
export SSH_USER="${SSH_USER:-eragon02424}"
export SSH_KEY_PATH="$KEY_FILE"

bashio::log.info "Starting MCP Shell v2.2.0 (SSH mode) on port 8767..."
bashio::log.info "SSH target: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"

exec python3 /server.py
