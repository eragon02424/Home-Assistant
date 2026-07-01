ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip eudev

COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY rootfs/usr/bin/mcp_serial_server.py /usr/bin/mcp_serial_server.py

RUN chmod +x /run.sh /usr/bin/mcp_serial_server.py

CMD ["/run.sh"]
