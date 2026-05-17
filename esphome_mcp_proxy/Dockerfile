ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install aiohttp
RUN apk add --no-cache py3-pip && \
    pip3 install --no-cache-dir --break-system-packages aiohttp

# Copy the startup script
COPY start.py /start.py

# Copy the custom integration source
COPY opt/ /opt/

# Copy s6-overlay service definition
COPY rootfs/ /

# Make run script executable
RUN chmod a+x /etc/s6-overlay/s6-rc.d/mcp-proxy/run
