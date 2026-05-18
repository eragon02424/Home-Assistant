FROM alpine:3.19

RUN apk add --no-cache curl tar python3 py3-pip

# Install Python MCP library for multiplexer
RUN pip install --no-cache-dir --break-system-packages mcp uvicorn

# Download latest github-mcp-server binary
RUN LATEST=$(curl -s https://api.github.com/repos/github/github-mcp-server/releases/latest | grep '"tag_name"' | cut -d'"' -f4) && \
    echo "Downloading github-mcp-server ${LATEST}..." && \
    curl -L "https://github.com/github/github-mcp-server/releases/download/${LATEST}/github-mcp-server_Linux_x86_64.tar.gz" \
    -o /tmp/github-mcp-server.tar.gz && \
    tar -xzf /tmp/github-mcp-server.tar.gz -C /usr/local/bin/ github-mcp-server && \
    chmod +x /usr/local/bin/github-mcp-server && \
    rm /tmp/github-mcp-server.tar.gz && \
    echo "Binary installed:" && \
    ls -la /usr/local/bin/github-mcp-server

COPY run.sh /run.sh
COPY multiplexer.py /multiplexer.py
RUN chmod +x /run.sh

EXPOSE 8766

CMD ["/run.sh"]
