#!/bin/sh
GRAFANA_URL=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['grafana_url'])")
GRAFANA_TOKEN=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['grafana_token'])")
export GRAFANA_URL
export GRAFANA_SERVICE_ACCOUNT_TOKEN="${GRAFANA_TOKEN}"
echo "Starting MCP Grafana..."
echo "Grafana URL: ${GRAFANA_URL}"
# Start mcp-grafana on internal port 8081
mcp-grafana -t streamable-http -address "127.0.0.1:8081" &
# Start nginx reverse proxy on port 8080 (rewrites Host header to localhost)
nginx -g "daemon off;"
