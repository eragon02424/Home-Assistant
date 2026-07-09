"""Real MCP (Model Context Protocol) server exposing everything the
REST API in api_routes.py already does, as proper MCP tools over
Streamable HTTP -- so this addon can be added as an actual MCP
connector, not just called via manual HTTP requests.

Uses the official Python MCP SDK's FastMCP (mcp.server.fastmcp),
transport="streamable-http". Every tool here is a thin wrapper around
the SAME device_manager / log_manager / job_manager / file_manager /
serial_flash / serial_info functions the REST API uses -- no logic is
duplicated, this is purely a second interface onto the same backend.

init() must be called once at startup (from server.py) to hand in the
already-constructed manager instances, since FastMCP tools are plain
module-level functions and have no natural way to receive per-request
dependencies otherwise.

DNS-rebinding protection (transport_security): the MCP SDK rejects any
request whose Host header isn't on an allowlist by default, returning
421 Misdirected Request -- confirmed by direct testing (a real MCP
client got 421 connecting via the Supervisor gateway IP,
172.30.32.1, since that's neither "localhost" nor 127.0.0.1). This
addon is only reachable on the internal HA/Supervisor network in the
first place (host_network: true, no public exposure), and real auth is
already enforced by our own Bearer-token check below, so disabling
this specific protection here is safe -- it isn't a substitute for
auth, it's a same-origin check that doesn't apply to our deployment
shape (matches the SDK's own documented guidance: disable it when
security is "managed at a different layer of your infrastructure").

Auth: Bearer token, same one as the REST API, checked via a small ASGI
middleware wrapped around the FastMCP Streamable HTTP app (see
get_asgi_app()).
"""
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

_LOGGER = logging.getLogger("mcp_esphome.mcp_tools")

mcp = FastMCP(
    "ESPHome MCP Server",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_device_manager = None
_log_manager = None
_job_manager = None
_bearer_token = ""


def init(device_manager, log_manager, job_manager, bearer_token: str):
    global _device_manager, _log_manager, _job_manager, _bearer_token
    _device_manager = device_manager
    _log_manager = log_manager
    _job_manager = job_manager
    _bearer_token = bearer_token


# ── Device status / history ─────────────────────────────────────

@mcp.tool()
def list_devices() -> list[dict]:
    """Lists all known ESPHome devices with their current online/offline
    state (from our own fast TCP-ping keepalive), last-seen time, and
    recent online/offline durations.
    """
    return _device_manager.list_devices()


@mcp.tool()
def get_last_seen(device_name: str) -> dict:
    """Returns a device's current online state and last-seen/last-disconnect
    timestamps.
    """
    result = _device_manager.get_last_seen(device_name)
    return result if result is not None else {"error": "device not found"}


@mcp.tool()
def get_online_offline_history(device_name: str, last_n: int = 10) -> dict:
    """Returns the last N completed online periods and the last N
    completed offline periods for a device, derived from persisted
    heartbeat history (survives addon/HA restarts).
    """
    result = _device_manager.get_online_offline_history(device_name, last_n)
    return result if result is not None else {"error": "device not found"}


# ── Debug logs ───────────────────────────────────────────────────

@mcp.tool()
def get_logs_recent(device_name: str, n: int = 100) -> list[dict]:
    """Returns the last N lines of a device's native ESPHome debug log
    (captured continuously while ESPHome reports the device online).
    """
    return _log_manager.get_recent(device_name, n)


@mcp.tool()
def get_logs_range(device_name: str, seconds: float) -> list[dict]:
    """Returns a device's debug log lines from the last `seconds`
    seconds, e.g. 600 for the last 10 minutes, 3600 for the last hour,
    86400 for the last day.
    """
    return _log_manager.get_range(device_name, seconds)


# ── Validate / compile / install (OTA) ──────────────────────────

@mcp.tool()
async def validate_config(device_name: str) -> dict:
    """Validates a device's YAML config (schema/type checking only, does
    NOT catch C++ build errors). Returns success, an ESPHome error code,
    and the full validation output.
    """
    return await _job_manager.validate_config(device_name)


@mcp.tool()
async def start_compile(device_name: str) -> dict:
    """Starts compiling a device's firmware WITHOUT flashing it. Returns
    a job_id to poll with get_job_status()/get_full_log()/get_error_summary().
    Catches real C++ build errors (unlike validate_config).
    """
    job_id = await _job_manager.start_compile(device_name)
    return {"job_id": job_id}


@mcp.tool()
async def start_install(device_name: str) -> dict:
    """Compiles AND flashes a device over WiFi (OTA). Returns a job_id.
    Internally chains a compile phase then an upload phase; poll with
    get_job_status() (has a flash_phase_started flag) and use
    get_flash_log() to see only the upload-phase output once it starts.
    """
    job_id = await _job_manager.start_install(device_name)
    return {"job_id": job_id}


@mcp.tool()
def get_job_status(job_id: str) -> dict:
    """Returns a compile/install job's current status, exit_code, and
    (for install jobs) whether the flash phase has started yet.
    """
    result = _job_manager.get_status(job_id)
    return result if result is not None else {"error": "job not found"}


@mcp.tool()
def get_error_summary(job_id: str) -> dict:
    """Returns a short, relevant excerpt of a failed job's output
    (around the actual error line, or the job's own reported error).
    Returns an error if the job hasn't failed or doesn't exist.
    """
    result = _job_manager.get_error_summary(job_id)
    return {"summary": result} if result is not None else {"error": "job not found or no error"}


@mcp.tool()
def get_full_log(job_id: str) -> dict:
    """Returns the complete accumulated output of a compile/install job."""
    result = _job_manager.get_full_log(job_id)
    return {"log": result} if result is not None else {"error": "job not found"}


@mcp.tool()
def get_flash_log(job_id: str) -> dict:
    """Returns ONLY the flash/upload-phase output of an install job,
    skipping the compile portion (which can be inspected separately).
    """
    result = _job_manager.get_flash_log(job_id)
    return result if result is not None else {"error": "job not found"}


# ── Serial flash (compile via ESPHome, flash via esptool directly) ─

@mcp.tool()
def fix_serial_upload_speed(device_name: str, speed: int = 115200) -> dict:
    """Secondary/incomplete workaround for ESP32-S2/S3 native-USB serial
    upload issues (esphome/issues#4090): forces
    esphome.platformio_options.upload_speed in the device's YAML. Kept
    as a harmless extra measure -- prepare_serial_flash() + flash_serial()
    below is the fix that actually works reliably.
    """
    return _device_manager.ensure_serial_upload_speed(device_name, speed)


@mcp.tool()
def prepare_serial_flash(device_name: str) -> dict:
    """Redirects a device's ESPHome build output to
    /config/esphome/.build/<name> (via the build_path YAML option) so
    the compiled firmware.factory.bin lands somewhere this addon can
    read it. Required once per device before flash_serial() can work;
    safe to call repeatedly.
    """
    return _device_manager.ensure_build_path(device_name)


@mcp.tool()
async def flash_serial(device_name: str, port: str) -> dict:
    """Flashes a device over a serial/USB port using esptool directly
    (--before no-reset), bypassing ESPHome's own upload mechanism
    entirely -- ESPHome's own serial upload resets ESP32-S2/S3 native-USB
    boards mid-flash and loses the port (esphome/issues#4090). Call
    prepare_serial_flash() and start_compile() first; this only writes
    the already-compiled firmware.factory.bin, it does not compile.
    `port` is a device path like "/dev/ttyACM0".
    """
    import serial_flash
    bin_path = _device_manager.get_factory_bin_path(device_name)
    try:
        return await serial_flash.flash_factory_bin(bin_path, port)
    except FileNotFoundError as err:
        return {"error": str(err)}


# ── Serial device discovery / chip info (read-only) ─────────────

@mcp.tool()
def list_serial_ports() -> list[dict]:
    """Lists serial devices currently connected to the Home Assistant
    host (port path, description, USB VID/PID) -- includes non-ESP
    devices too (e.g. Zigbee dongles), so you can tell them apart before
    picking a port for get_serial_chip_info() or flash_serial().
    """
    import serial_info
    return serial_info.list_serial_ports()


@mcp.tool()
async def get_serial_chip_info(port: str) -> dict:
    """Queries a USB-connected ESP's technical details directly via
    esptool: chip type/revision, feature list, embedded flash size,
    embedded PSRAM size (None if the chip has none), crystal frequency,
    USB mode, and MAC address. Read-only (never writes flash).

    WARNING: esptool's own chip-detection handshake ends by resetting
    the target. On ESP32-S2/S3 native-USB boards (no external
    USB-serial chip) this can knock the device out of flashing mode for
    an unpredictable time (confirmed: minutes to hours in testing) --
    only call this when you actually need the chip info, not as a
    routine check before flash_serial().
    """
    import serial_info
    return await serial_info.get_chip_info(port)


# ── File access (device YAMLs, templates, custom components) ────

@mcp.tool()
def list_esphome_files(path: str = "") -> dict:
    """Lists the contents of a directory under /config/esphome/ (device
    YAMLs live at the root; templates/packages and custom components
    live in subdirectories like components/<name>/).
    """
    import file_manager
    return file_manager.list_files(path)


@mcp.tool()
def read_esphome_file(path: str) -> dict:
    """Reads any text file under /config/esphome/ -- device YAMLs,
    package/template YAMLs (e.g. ZZVorlageDeepSleepSettingsV2.yaml), or
    custom component source (components/<name>/__init__.py, *.cpp, *.h).
    """
    import file_manager
    return file_manager.read_file(path)


@mcp.tool()
def write_esphome_file(path: str, content: str, create_dirs: bool = False) -> dict:
    """Writes (creates or overwrites) a text file under /config/esphome/.
    Set create_dirs=True when writing into a brand-new subdirectory
    (e.g. a new custom component or template that doesn't exist yet).
    """
    import file_manager
    return file_manager.write_file(path, content, create_dirs)


# ── ASGI app with bearer-token auth ──────────────────────────────

def get_asgi_app():
    """Returns the Streamable HTTP ASGI app, wrapped with the same
    Bearer-token check as the REST API.
    """
    inner_app = mcp.streamable_http_app()
    token = _bearer_token

    async def app(scope, receive, send):
        if scope["type"] == "http" and token:
            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode("utf-8", errors="ignore")
            if auth != f"Bearer {token}":
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error": "unauthorized"}',
                })
                return
        await inner_app(scope, receive, send)

    return app
