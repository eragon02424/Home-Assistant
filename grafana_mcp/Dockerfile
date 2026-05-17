FROM python:3.12-alpine
RUN pip install --no-cache-dir mcp-grafana && \
    which mcp-grafana && \
    ls -la $(which mcp-grafana)
COPY run.sh /run.sh
RUN chmod +x /run.sh
EXPOSE 8080
CMD ["/run.sh"]
