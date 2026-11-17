"""
MCP OneDrive für Home Assistant v0.1.0

Die Tuer fuer Claude zum OneDrive-Bildabruf. Dieser Server wartet nie: Er
reicht Suche, Auftraege und Status an die Token-API des OneDrive SyncServers
(Add-on onedrive_syncserver, Port 8776) durch. Dort liegen Token-Pruefung,
Freigabe durch den Besitzer, Download und Download-Link; hier steckt keine
Fachlogik.

Der Endpunkt-Token (ein Token = ein Endpunkt mit festen Ordnern und festen
Berechtigungen) steht in den Add-on-Optionen und wird nie an Claude
zurueckgegeben. Dateien laufen nicht durch diesen Server: Bei "bereit" liefert
der SyncServer einen Download-Link, den ein Programm mit Shell (z. B. Claude
Code ueber den MCP-Server "MCP Claude Code") direkt laedt.
"""

import functools as _functools
import inspect as _inspect
import logging
import os

import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as _ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError as _MCPError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

VERSION = "0.1.0"
PORT = 8775

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_onedrive")
for _noisy in ("uvicorn.access", "uvicorn.error", "httpx", "mcp", "mcp.server", "mcp.server.fastmcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

TOKEN = os.environ.get("MCP_TOKEN", "")
SYNC_API_URL = (os.environ.get("SYNC_API_URL") or "").rstrip("/")
ENDPOINT_TOKEN = os.environ.get("ENDPOINT_TOKEN", "")

log.info("Token auth: %s", "enabled" if TOKEN else "disabled")
log.info("SyncServer API: %s (Endpunkt-Token %s)", SYNC_API_URL, "gesetzt" if ENDPOINT_TOKEN else "FEHLT")


async def _api(method: str, path: str, body: dict | None = None, timeout: float = 30) -> dict:
    """Ruft die Token-API des SyncServers auf; Fehler werden mit der Originalmeldung weitergegeben."""
    if not SYNC_API_URL:
        raise RuntimeError("syncserver_api_url ist in den Add-on-Optionen nicht gesetzt")
    if not ENDPOINT_TOKEN:
        raise RuntimeError("endpoint_token ist in den Add-on-Optionen nicht gesetzt")
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, SYNC_API_URL + path,
                headers={"Authorization": f"Bearer {ENDPOINT_TOKEN}"},
                json=body,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"OneDrive SyncServer nicht erreichbar: {type(e).__name__} {e}") from e
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:300]}
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {data.get('error', data)}")
    return data


mcp = MCPServer(
    version=VERSION,
    name="MCP OneDrive",
    instructions=(
        "Find files in fixed OneDrive folders by file name and request a download of ONE file. "
        "Flow: search_file(filename) returns the full path(s); request_download(path) creates a job; "
        "get_job(job_id) shows its state. This server never waits. Every response about a job contains "
        "wait_seconds: the number of seconds to wait before asking again (set a timer, do not poll faster). "
        "Job status values: waiting_for_approval (the owner must approve the download on a page in Home "
        "Assistant, a preview is shown there; do not ask for credentials), processing, ready, and the final "
        "states rejected, expired, failed (see error). When ready, the response contains download_url "
        "(valid until expires_at, usable several times, no further authentication) and sha256. The file is NOT "
        "transferred through this server: fetch download_url with a program that has a shell, for example a "
        "Claude Code job started through the Claude Code MCP server (curl the URL, compare the sha256, then "
        "read the file). Which folders and actions this connection allows is shown by get_endpoint_info."
    ),
)

# MCP SDK 2.x: Fehlermeldungen von Tools wie in 1.x an das Modell durchreichen.


def _visible_errors(fn):
    if _inspect.iscoroutinefunction(fn):
        @_functools.wraps(fn)
        async def _wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    else:
        @_functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    return _wrapper


_mcp_tool_orig = mcp.tool


def _mcp_tool(*args, **kwargs):
    decorator = _mcp_tool_orig(*args, **kwargs)
    return lambda fn: decorator(_visible_errors(fn))


mcp.tool = _mcp_tool


@mcp.tool()
async def get_endpoint_info() -> dict:
    """
    Show what this connection may do: name, the fixed OneDrive folders it can search, and the
    permission per action (list, download, write, delete) as blocked, always or confirm
    (confirm = the owner approves each download). Write and delete are not available.
    """
    return await _api("GET", "/v1/info")


@mcp.tool()
async def search_file(filename: str, deep: bool = False) -> dict:
    """
    Search a file by its exact file name (no path, upper/lower case does not matter) in the fixed
    folders of this connection. Fresh files are found immediately.

    Args:
        filename: The file name only, e.g. "IMG_20261117_101530.jpg". Names with a path are rejected.
        deep: If true, also search subfolders when a hit exists in the main folder (default: only
            search subfolders when nothing was found in the main folder).

    Returns:
        found (number of hits) and locations (list with the full path, size, modified). Use the
        path of a hit for request_download. search_incomplete=true means the search could not look
        everywhere (limit reached or the folder list is not ready yet); try again later.
    """
    return await _api("POST", "/v1/search", {"filename": filename, "deep": bool(deep)}, timeout=60)


@mcp.tool()
async def request_download(path: str) -> dict:
    """
    Request the download of ONE file by its full path (from search_file). Returns immediately with
    job_id, status and wait_seconds. If the connection requires approval, status is
    waiting_for_approval and the owner decides on a page in Home Assistant (10 minutes, then
    expired); the same file requested again returns the same job. Poll with get_job after
    wait_seconds seconds.
    """
    return await _api("POST", "/v1/jobs", {"action": "download", "path": path})


@mcp.tool()
async def get_job(job_id: str) -> dict:
    """
    Get the state of a download job. status: waiting_for_approval, processing, ready, rejected,
    expired or failed. wait_seconds says how long to wait before asking again (0 = final). When
    ready: download_url (fetch it with a program that has a shell; valid until expires_at,
    several downloads allowed) and sha256 of the file. A final state that was fetched once is
    removed after a few minutes (then the job is unknown).
    """
    return await _api("GET", f"/v1/jobs/{job_id}")


class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not TOKEN:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        token_param = request.query_params.get("token", "")
        if auth_header == f"Bearer {TOKEN}" or token_param == TOKEN:
            return await call_next(request)
        log.warning("Unauthorized request from %s", request.client)
        return Response("Unauthorized", status_code=401)


# DNS-Rebinding-Schutz aus, damit Anfragen über LAN-IP/Proxy akzeptiert werden
app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
