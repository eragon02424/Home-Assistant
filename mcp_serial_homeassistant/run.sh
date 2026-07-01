#!/usr/bin/with-contenv bashio

bashio::log.info "Starting MCP Serial HomeAssistant v1.0.1..."

PORT=$(bashio::config 'port')
export SERIAL_PORT_CONFIG=$(bashio::config 'port')
export MCP_PORT="${PORT}"

exec python3 /server.py
