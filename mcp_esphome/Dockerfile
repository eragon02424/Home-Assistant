FROM python:3.12-alpine

WORKDIR /app

RUN pip install --no-cache-dir aiohttp aioesphomeapi

COPY rootfs/app/ /app/

EXPOSE 8090

CMD ["python", "server.py"]
