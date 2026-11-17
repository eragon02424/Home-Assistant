#!/usr/bin/with-contenv bashio

TOKEN_FILE="/data/mcp_token"

if [ ! -f "$TOKEN_FILE" ]; then
    NEW_TOKEN=$(cat /proc/sys/kernel/random/uuid)
    echo "$NEW_TOKEN" > "$TOKEN_FILE"
    bashio::log.info "Generated new MCP token: ${NEW_TOKEN}"
else
    bashio::log.info "Using existing MCP token: $(cat $TOKEN_FILE)"
fi

KEY_FILE="$(bashio::config 'ssh_key_path')"
if [ ! -f "$KEY_FILE" ]; then
    bashio::log.warning "SSH private key not found at $KEY_FILE - connections will fail."
fi

export MCP_TOKEN=$(cat "$TOKEN_FILE")
export SSH_HOST=$(bashio::config 'ssh_host')
export SSH_PORT=$(bashio::config 'ssh_port')
export SSH_USER=$(bashio::config 'ssh_user')
export SSH_KEY_PATH="$KEY_FILE"
export WORKSPACE_PATH=$(bashio::config 'workspace_path')
export CLAUDE_BINARY=$(bashio::config 'claude_binary')
export LOGIN_CHECK_MINUTES=$(bashio::config 'login_check_minutes')

bashio::log.info "Starting MCP Claude Code v0.1.0 on port 8774..."
bashio::log.info "SSH target: ${SSH_USER}@${SSH_HOST}:${SSH_PORT}"
bashio::log.info "Workspace: ${WORKSPACE_PATH}"

exec python3 /server.py
