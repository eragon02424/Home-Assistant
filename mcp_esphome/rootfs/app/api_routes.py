"""HTTP API routes exposed by MCP ESPHome."""
import logging

from aiohttp import web

from job_manager import JobManager

_LOGGER = logging.getLogger("mcp_esphome.api")


def setup_routes(app: web.Application):
    device_manager = app["device_manager"]
    log_manager = app["log_manager"]
    job_manager = JobManager(device_manager.esphome_dashboard_url)
    app["job_manager"] = job_manager
    bearer_token = app.get("bearer_token", "")

    @web.middleware
    async def auth_middleware(request, handler):
        if bearer_token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header != f"Bearer {bearer_token}":
                return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def list_devices(request):
        return web.json_response(device_manager.list_devices())

    async def get_last_seen(request):
        name = request.match_info["device_name"]
        result = device_manager.get_last_seen(name)
        if result is None:
            return web.json_response({"error": "device not found"}, status=404)
        return web.json_response(result)

    async def get_online_offline_history(request):
        name = request.match_info["device_name"]
        n = int(request.query.get("last_n", 10))
        result = device_manager.get_online_offline_history(name, n)
        if result is None:
            return web.json_response({"error": "device not found"}, status=404)
        return web.json_response(result)

    async def get_logs_recent(request):
        name = request.match_info["device_name"]
        n = int(request.query.get("n", 100))
        return web.json_response(log_manager.get_recent(name, n))

    async def get_logs_range(request):
        name = request.match_info["device_name"]
        try:
            seconds = float(request.query["seconds"])
        except (KeyError, ValueError):
            return web.json_response(
                {"error": "query param 'seconds' is required, e.g. seconds=3600 for the last hour"},
                status=400,
            )
        return web.json_response(log_manager.get_range(name, seconds))

    async def validate_config(request):
        name = request.match_info["device_name"]
        try:
            result = await job_manager.validate_config(name)
        except Exception as err:
            _LOGGER.error("Validate failed for %s: %s", name, err)
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response(result)

    async def start_compile(request):
        name = request.match_info["device_name"]
        try:
            job_id = await job_manager.start_compile(name)
        except Exception as err:
            _LOGGER.error("Compile start failed for %s: %s", name, err)
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response({"job_id": job_id})

    async def start_install(request):
        name = request.match_info["device_name"]
        try:
            job_id = await job_manager.start_install(name)
        except Exception as err:
            _LOGGER.error("Install (OTA) start failed for %s: %s", name, err)
            return web.json_response({"error": str(err)}, status=502)
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
            return web.json_response({"error": "job not found or no error"}, status=404)
        return web.json_response({"summary": summary})

    async def get_full_log(request):
        job_id = request.match_info["job_id"]
        log = job_manager.get_full_log(job_id)
        if log is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response({"log": log})

    async def get_flash_log(request):
        job_id = request.match_info["job_id"]
        result = job_manager.get_flash_log(job_id)
        if result is None:
            return web.json_response({"error": "job not found"}, status=404)
        return web.json_response(result)

    async def health(request):
        return web.json_response({"status": "ok", "devices": len(device_manager.devices)})

    app.router.add_get("/health", health)
    app.router.add_get("/devices", list_devices)
    app.router.add_get("/devices/{device_name}/last_seen", get_last_seen)
    app.router.add_get("/devices/{device_name}/history", get_online_offline_history)
    app.router.add_get("/devices/{device_name}/logs/recent", get_logs_recent)
    app.router.add_get("/devices/{device_name}/logs/range", get_logs_range)
    app.router.add_post("/devices/{device_name}/validate", validate_config)
    app.router.add_post("/devices/{device_name}/compile", start_compile)
    app.router.add_post("/devices/{device_name}/install", start_install)
    app.router.add_get("/jobs/{job_id}/status", get_job_status)
    app.router.add_get("/jobs/{job_id}/error_summary", get_error_summary)
    app.router.add_get("/jobs/{job_id}/full_log", get_full_log)
    app.router.add_get("/jobs/{job_id}/flash_log", get_flash_log)
