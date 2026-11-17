#!/usr/bin/with-contenv bashio

export GROCY_HOST=$(bashio::config 'grocy_host')
export GROCY_LOCATION_ID=$(bashio::config 'location_id')
export GROCY_QU_PURCHASE=$(bashio::config 'qu_id_purchase')
export GROCY_QU_STOCK=$(bashio::config 'qu_id_stock')

# OneDrive-Abruf der Rezeptbilder ueber den Endpunkt 'scanner' des OneDrive SyncServers
export ONEDRIVE_API_URL=$(bashio::config 'onedrive_api_url')
ONEDRIVE_TOKEN_VALUE=$(bashio::config 'onedrive_token')
if [ -z "${ONEDRIVE_TOKEN_VALUE}" ] || [ "${ONEDRIVE_TOKEN_VALUE}" = "null" ]; then
    bashio::log.warning "onedrive_token ist nicht gesetzt - Rezeptbilder aus OneDrive (bildname) schlagen fehl."
    ONEDRIVE_TOKEN_VALUE=""
fi
export ONEDRIVE_TOKEN="${ONEDRIVE_TOKEN_VALUE}"

bashio::log.info "Starting MCP Shopping Products on port 8770, Grocy host: ${GROCY_HOST}"

exec python3 /main.py
