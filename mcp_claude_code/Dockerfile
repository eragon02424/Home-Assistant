ARG BUILD_FROM
FROM $BUILD_FROM

# Python und pip installieren
RUN apk add --no-cache python3 py3-pip

# Python-Abhängigkeiten installieren
COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

# Anwendungsdateien kopieren
COPY run.sh /run.sh
COPY server.py /server.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
