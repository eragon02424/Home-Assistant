#!/usr/bin/with-contenv bashio

export GROCY_HOST=$(bashio::config 'grocy_host')
export GROCY_LOCATION_ID=$(bashio::config 'location_id')
export GROCY_QU_PURCHASE=$(bashio::config 'qu_id_purchase')
export GROCY_QU_STOCK=$(bashio::config 'qu_id_stock')

bashio::log.info "Starting MCP Shopping Products on port 8770, Grocy host: ${GROCY_HOST}"

exec python3 /server.py
