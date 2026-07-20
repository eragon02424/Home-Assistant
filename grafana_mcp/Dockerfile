FROM python:3.12-alpine
RUN apk add --no-cache nginx nginx-mod-http-headers-more && \
    pip install --no-cache-dir mcp-grafana && \
    which mcp-grafana && \
    ls -la $(which mcp-grafana)
COPY run.sh /run.sh
COPY nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /run.sh
EXPOSE 8080
CMD ["/run.sh"]
