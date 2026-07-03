#!/usr/bin/with-contenv bashio

# Warte bis /config/nginx/site-confs existiert (s6 init legt es an)
mkdir -p /config/nginx/site-confs

# Ingress nginx config kopieren
cp /ingress.conf.tpl /config/nginx/site-confs/ingress.conf

# Grocy Sprache aus HA Addon Config
CULTURE=$(bashio::config 'culture')
echo "GROCY_CULTURE=${CULTURE}" >> /etc/environment
echo "GROCY_AUTH_CLASS=Grocy\\Middleware\\ReverseProxyAuthMiddleware" >> /etc/environment

bashio::log.info "Grocy: culture=${CULTURE}, auth=ReverseProxy"
