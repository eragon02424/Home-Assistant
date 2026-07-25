ARG BUILD_FROM
FROM $BUILD_FROM

# Install Python and pip
RUN apk add --no-cache python3 py3-pip

# Install Python dependencies
COPY requirements.txt /tmp/
RUN pip3 install --break-system-packages -r /tmp/requirements.txt

# Copy application files
COPY run.sh /run.sh
COPY server.py /server.py

RUN chmod +x /run.sh

CMD ["/run.sh"]
