#!/bin/sh

GITHUB_TOKEN=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['github_token'])")
PORT=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['port'])")

echo "Starting MCP GitHub..."
echo "Port: ${PORT}"

exec env GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_TOKEN}" /usr/local/bin/github-mcp-server http --port "${PORT}"
