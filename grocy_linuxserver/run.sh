#!/usr/bin/with-contenv bashio

# Set Grocy culture from HA addon config
CULTURE=$(bashio::config 'culture')
export GROCY_CULTURE="${CULTURE}"

# Enable reverse proxy auth so no separate Grocy login is needed
export GROCY_AUTH_CLASS="Grocy\\Middleware\\ReverseProxyAuthMiddleware"

bashio::log.info "Grocy starting with culture=${CULTURE}, auth=ReverseProxy"
