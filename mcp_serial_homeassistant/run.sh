#!/usr/bin/with-contenv bashio

bashio::log.info "Starting MCP Serial HomeAssistant v1.0.0..."

exec python3 /server.py
