ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache eudev

COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY server.py /server.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
