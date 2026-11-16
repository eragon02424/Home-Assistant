ARG BUILD_FROM
FROM $BUILD_FROM

# ffmpeg für Bilder/Ton, nodejs als JavaScript-Laufzeit für yt-dlp (YouTube)
RUN apk add --no-cache ffmpeg nodejs

COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

COPY run.sh /run.sh
COPY server.py /server.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
