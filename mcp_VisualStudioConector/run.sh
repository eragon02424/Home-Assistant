#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"
KEY_DIR="/config/mcp_vs_ssh"
KEY_FILE="$KEY_DIR/mcp_vs_key"

if [ ! -f "$TOKEN_FILE" ]; then
    NEW_TOKEN=$(cat /proc/sys/kernel/random/uuid)
    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    bashio::log.info "Generated new MCP token: ${NEW_TOKEN}"
else
    bashio::log.info "Using existing MCP token: $(cat $TOKEN_FILE)"
fi

if [ ! -f "$KEY_FILE" ]; then
    bashio::log.warning "SSH private key not found at $KEY_FILE"
    bashio::log.warning "Fallback: password auth via SSH_PASSWORD env (if set), otherwise connections will fail."
else
    chmod 600 "$KEY_FILE"
fi

export MCP_TOKEN=$(cat "$TOKEN_FILE")
export SSH_HOST=$(bashio::config 'ssh_host')
export SSH_PORT=$(bashio::config 'ssh_port')
export SSH_USER=$(bashio::config 'ssh_user')
export WORKSPACE_PATH=$(bashio::config 'workspace_path')
export CLAUDE_BINARY=$(bashio::config 'claude_binary')
export SSH_KEY_PATH="$KEY_FILE"

bashio::log.info "Starting MCP Visual Studio Connector v0.1.4 on port 8768..."
bashio::log.info "SSH target: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
bashio::log.info "Workspace: ${WORKSPACE_PATH}"

exec python3 /server.py
