FROM python:3.12-alpine

WORKDIR /app

# v0.4.0 — force full rebuild
COPY rootfs/app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY rootfs/app/ /app/

EXPOSE 8090

CMD ["python", "server.py"]
