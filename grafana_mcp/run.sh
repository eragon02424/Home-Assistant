#!/bin/sh
GRAFANA_URL=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['grafana_url'])")
GRAFANA_TOKEN=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['grafana_token'])")
PORT=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['port'])")
export GRAFANA_URL
export GRAFANA_SERVICE_ACCOUNT_TOKEN="${GRAFANA_TOKEN}"
echo "Starting Grafana MCP Server..."
echo "Grafana URL: ${GRAFANA_URL}"
echo "Port: ${PORT}"
exec mcp-grafana -t streamable-http -address "0.0.0.0:${PORT}"
