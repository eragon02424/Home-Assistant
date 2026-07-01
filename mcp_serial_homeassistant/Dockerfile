ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip py3-serial eudev

RUN pip3 install --break-system-packages pyudev

COPY rootfs /

RUN chmod +x /usr/bin/mcp_serial_server.py

CMD ["/usr/bin/mcp_serial_server.py"]
