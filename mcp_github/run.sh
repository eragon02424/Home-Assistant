#!/bin/sh

GITHUB_TOKEN=$(cat /data/options.json | python3 -c "import sys,json; print(json.load(sys.stdin)['github_token'])")

export GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_TOKEN}"

echo "Starting MCP GitHub Extended..."

# Start official github-mcp-server on internal port 8767
env GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_TOKEN}" /usr/local/bin/github-mcp-server http --port 8767 &
UPSTREAM_PID=$!
echo "Official github-mcp-server started (PID ${UPSTREAM_PID}) on port 8767"

# Wait for upstream to be ready
echo "Waiting for upstream..."
sleep 3

# Start multiplexer on external port 8766
exec env \
  GITHUB_PERSONAL_ACCESS_TOKEN="${GITHUB_TOKEN}" \
  UPSTREAM_PORT=8767 \
  LISTEN_PORT=8766 \
  python3 /multiplexer.py
