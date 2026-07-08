ARG BUILD_FROM
FROM $BUILD_FROM

# Install dependencies
RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-flask \
    curl \
    bash \
    findutils \
    tzdata

# onedrive: Alpine stable ships an outdated version (2.4.25) with a known bug
# (AADSTS70000 on auth-code exchange, fixed upstream in 2.5.10+). We pull
# only this one package from Alpine edge/community without switching the
# whole system to edge, to avoid the library-mismatch risks Alpine warns
# about when mixing stable + edge repos wholesale.
RUN apk add --no-cache -X https://dl-cdn.alpinelinux.org/alpine/edge/community onedrive

# Install Python deps
RUN pip3 install flask flask-cors requests --break-system-packages

# Copy addon files
COPY rootfs /

RUN chmod +x /run.sh

CMD ["/run.sh"]
