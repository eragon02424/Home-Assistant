"""MCP ESPHome - Main server entry point."""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from aiohttp import web

from device_manager import DeviceManager
from api_routes import setup_routes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
_LOGGER = logging.getLogger("mcp_esphome")

OPTIONS_FILE = Path("/data/options.json")


def load_options() -> dict:
    if OPTIONS_FILE.exists():
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    return {}


async def main():
    opts = load_options()

    esphome_dashboard_url = opts.get("esphome_dashboard_url", "http://localhost:6052")
    bearer_token = opts.get("bearer_token", "")
    port = opts.get("port", 8090)
    log_retention_hours = opts.get("log_retention_hours", 24)
    heartbeat_retention_days = opts.get("heartbeat_retention_days", 30)
    mqtt_host = opts.get("mqtt_host", "core-mosquitto")
    mqtt_port = opts.get("mqtt_port", 1883)
    mqtt_username = opts.get("mqtt_username", "")
    mqtt_password = opts.get("mqtt_password", "")

    _LOGGER.info("=" * 60)
    _LOGGER.info("MCP ESPHome starting")
    _LOGGER.info("Dashboard URL: %s", esphome_dashboard_url)
    _LOGGER.info("Port: %s", port)
    _LOGGER.info("MQTT: %s:%s", mqtt_host, mqtt_port)
    _LOGGER.info("=" * 60)

    device_manager = DeviceManager(
        esphome_dashboard_url=esphome_dashboard_url,
        log_retention_hours=log_retention_hours,
        heartbeat_retention_days=heartbeat_retention_days,
        mqtt_host=mqtt_host,
        mqtt_port=mqtt_port,
        mqtt_username=mqtt_username,
        mqtt_password=mqtt_password,
    )

    asyncio.create_task(device_manager.run_discovery_loop())

    app = web.Application()
    app["device_manager"] = device_manager
    app["bearer_token"] = bearer_token
    setup_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    _LOGGER.info("MCP ESPHome API listening on 0.0.0.0:%s", port)

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
