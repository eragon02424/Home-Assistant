ARG BUILD_FROM
FROM $BUILD_FROM

# zbar + imagemagick fuer Barcode-Dekodierung aus Fotos (getestet und bestaetigt funktionierend),
# python3/pip fuer den MCP+Ingress Server
RUN apk add --no-cache python3 py3-pip zbar imagemagick imagemagick-jpeg

COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY server.py /server.py
COPY static /static

RUN chmod +x /run.sh

CMD ["/run.sh"]
