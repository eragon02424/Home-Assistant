"""MCP ESPHome - Main server entry point.

Runs TWO servers concurrently in the same process:
  - The existing REST API (aiohttp) on `port` (default 8090) -- kept
    for manual/curl-style access and internal testing.
  - A real MCP server (Streamable HTTP, via the official Python MCP
    SDK's FastMCP) on `mcp_port` (default 8091) -- this is what should
    be added as an actual MCP connector, exposing every capability as
    a proper tool with a JSON schema, not just an HTTP endpoint someone
    has to know how to call.
Both wrap the exact same device_manager/log_manager/job_manager/
file_manager/serial_flash logic; nothing is duplicated between them.
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import aiohttp
import uvicorn
from aiohttp import web

from device_manager import DeviceManager
from log_manager import LogManager
from job_manager import JobManager
from api_routes import setup_routes
import mcp_tools

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


async def fetch_ha_timezone() -> str | None:
    """Reads Home Assistant's configured timezone via the Supervisor's
    Core API proxy. Used to give aioesphomeapi an explicit timezone so
    it skips its own get_local_timezone() lookup entirely (see
    log_manager.py docstring for why that lookup is unsafe here).
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "http://supervisor/core/api/config",
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("time_zone")
    except Exception as err:
        _LOGGER.warning("Could not fetch HA timezone from Supervisor: %s", err)
        return None


async def main():
    opts = load_options()

    esphome_dashboard_url = opts.get("esphome_dashboard_url", "http://localhost:6052")
    bearer_token = opts.get("bearer_token", "")
    port = opts.get("port", 8090)
    mcp_port = opts.get("mcp_port", 8091)
    heartbeat_retention_days = opts.get("heartbeat_retention_days", 30)
    keepalive_interval = opts.get("keepalive_interval", 10)
    keepalive_retries = opts.get("keepalive_retries", 5)
    keepalive_ping_timeout_ms = opts.get("keepalive_ping_timeout_ms", 500)
    keepalive_max_backoff_seconds = opts.get("keepalive_max_backoff_seconds", 21600)
    log_retention_days = opts.get("log_retention_days", 10)
    configured_timezone = opts.get("timezone", "") or None

    timezone = configured_timezone or await fetch_ha_timezone()

    log_manager = LogManager(retention_days=log_retention_days, timezone=timezone)

    device_manager = DeviceManager(
        esphome_dashboard_url=esphome_dashboard_url,
        heartbeat_retention_days=heartbeat_retention_days,
        keepalive_interval=keepalive_interval,
        keepalive_retries=keepalive_retries,
        keepalive_ping_timeout_ms=keepalive_ping_timeout_ms,
        keepalive_max_backoff_seconds=keepalive_max_backoff_seconds,
        bearer_token=bearer_token,
        log_manager=log_manager,
    )

    job_manager = JobManager(esphome_dashboard_url=esphome_dashboard_url)

    _LOGGER.info("=" * 60)
    _LOGGER.info("MCP ESPHome starting")
    _LOGGER.info("Dashboard URL: %s", esphome_dashboard_url)
    _LOGGER.info("REST API port: %s", port)
    _LOGGER.info("MCP (Streamable HTTP) port: %s", mcp_port)
    _LOGGER.info("Keepalive: interval=%ds retries=%d timeout=%dms max_backoff=%ds",
                 keepalive_interval, keepalive_retries, keepalive_ping_timeout_ms,
                 keepalive_max_backoff_seconds)
    _LOGGER.info("Log retention: %d days", log_retention_days)
    _LOGGER.info("Timezone for aioesphomeapi clients: %s", timezone)
    _LOGGER.info("Bearer Token: %s", device_manager.bearer_token)
    _LOGGER.info("=" * 60)

    # mDNS listener must be running before discovery starts tasks, so that
    # wake_events exist and announces during discovery aren't lost.
    await device_manager.start_mdns_listener()

    # Share the addon's single AsyncZeroconf instance with LogManager so
    # every aioesphomeapi log-subscription client reuses it instead of
    # each spinning up (and tearing down) its own for .local resolution.
    log_manager.set_zeroconf_instance(device_manager.get_zeroconf_instance())

    # Persistent subscribe_events listener for firmware job output/status
    # (compile + install, including auto-discovering the chained upload
    # job for install() -- see job_manager.py docstring).
    await job_manager.start()

    _LOGGER.info("Running initial device discovery...")
    await device_manager.run_initial_discovery()
    _LOGGER.info("Initial discovery complete — %d device(s) with keepalive tasks",
                 len(device_manager.devices))

    asyncio.create_task(device_manager.run_discovery_loop())
    asyncio.create_task(log_manager.run_prune_loop())

    # ── REST API (existing) ──────────────────────────────────
    app = web.Application()
    app["device_manager"] = device_manager
    app["log_manager"] = log_manager
    app["job_manager"] = job_manager
    app["bearer_token"] = device_manager.bearer_token
    setup_routes(app)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _LOGGER.info("REST API listening on 0.0.0.0:%s", port)

    # ── Real MCP server (Streamable HTTP) ────────────────────
    mcp_tools.init(device_manager, log_manager, job_manager, device_manager.bearer_token)
    uv_config = uvicorn.Config(
        mcp_tools.get_asgi_app(),
        host="0.0.0.0",
        port=mcp_port,
        log_level="info",
    )
    uv_server = uvicorn.Server(uv_config)
    asyncio.create_task(uv_server.serve())
    _LOGGER.info("MCP server (Streamable HTTP) listening on 0.0.0.0:%s/mcp", mcp_port)

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
