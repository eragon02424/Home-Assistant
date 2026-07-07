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
    tzdata \
    onedrive

# Install Python deps
RUN pip3 install flask flask-cors requests --break-system-packages

# Copy addon files
COPY rootfs /

RUN chmod +x /run.sh

CMD ["/run.sh"]
