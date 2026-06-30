"""HTTP API routes exposed by MCP ESPHome.

These map directly to the MCP tool functions Claude will call.
"""
import logging

from aiohttp import web

from job_manager import JobManager

_LOGGER = logging.getLogger("mcp_esphome.api")


def _check_auth(request: web.Request) -> bool:
    expected = request.app.get("bearer_token", "")
    if not expected:
        return True  # no auth configured
    auth_header = request.headers.get("Authorization", "")
    return auth_header == f"Bearer {expected}"


def setup_routes(app: web.Application):
    device_manager = app["device_manager"]
    job_manager = JobManager(device_manager.esphome_dashboard_url)
    app["job_manager"] = job_manager

    async def auth_middleware(request, handler):
        if not _check_auth(request):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    # ── Device discovery / status ──────────────────────────────

    async def list_devices(request):
        return web.json_response(device_manager.list_devices())

    async def get_last_seen(request):
        name = request.match_info["device_name"]
        result = device_manager.get_last_seen(name)
        if result is None:
            return web.json_response({"error": "device not found"}, status=404)
        return web.json_response(result)

    async def get_uptime_pattern(request):
        name = request.match_info["device_name"]
        n = int(request.query.get("last_n_cycles", 10))
        return web.json_response(device_manager.get_uptime_pattern(name, n))

    # ── Logs ─────────────────────────────────────────────────────

    async def get_device_logs(request):
        name = request.match_info["device_name"]
        return web.json_response(device_manager.get_device_logs(name))

    # ── Compile / Install jobs ──────────────────────────────────

    async def start_compile(request):
        name = request.match_info["device_name"]
        job_id = await job_manager.start_compile(name)
        return web.json_response({"job_id": job_id})

    async def start_install(request):
        name = request.match_info["device_name"]
        job_id = await job_manager.start_install(name)
        return web.json_response({"job_id": job_id})

    async def get_job_status(request):
        job_id = request.match_info["job_id"]
        status = job_manager.get_status(job_id)
        if status is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(status)

    async def get_error_summary(request):
        job_id = request.match_info["job_id"]
        summary = job_manager.get_error_summary(job_id)
        if summary is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response({"summary": summary})

    async def get_full_log(request):
        job_id = request.match_info["job_id"]
        log = job_manager.get_full_log(job_id)
        if log is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response({"log": log})

    async def health(request):
        return web.json_response({"status": "ok", "devices": len(device_manager.devices)})

    app.router.add_get("/health", health)
    app.router.add_get("/devices", list_devices)
    app.router.add_get("/devices/{device_name}/last_seen", get_last_seen)
    app.router.add_get("/devices/{device_name}/uptime_pattern", get_uptime_pattern)
    app.router.add_get("/devices/{device_name}/logs", get_device_logs)
    app.router.add_post("/devices/{device_name}/compile", start_compile)
    app.router.add_post("/devices/{device_name}/install", start_install)
    app.router.add_get("/jobs/{job_id}/status", get_job_status)
    app.router.add_get("/jobs/{job_id}/error_summary", get_error_summary)
    app.router.add_get("/jobs/{job_id}/full_log", get_full_log)
