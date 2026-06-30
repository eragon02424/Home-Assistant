"""
MCP Shell Server for Home Assistant v2.0.0
Provides execute_command tool for running shell commands via SSH on the real
Home Assistant host (Advanced SSH & Web Terminal add-on), giving access to
the `ha` CLI, `docker`, and the full host filesystem (/addons, /share, etc.)
— exactly as if the user typed the command themselves over SSH.

Authentication to clients: Bearer token (same pattern as before).
Authentication to the HA host: SSH key-based auth (no password used).
"""

import asyncio
import os

import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Config from environment ───────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")

SSH_HOST = os.environ.get("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/data/ssh_key/mcp_shell_key")

print(f"[MCP Shell] Token auth: {'enabled' if TOKEN else 'disabled'}")
print(f"[MCP Shell] SSH target: {SSH_USER}@{SSH_HOST}:{SSH_PORT}")

# ── Persistent SSH connection (reconnect on failure) ──────────────────────────

_ssh_conn = None
_ssh_lock = asyncio.Lock()


async def _get_connection():
    global _ssh_conn
    async with _ssh_lock:
        if _ssh_conn is not None and not _ssh_conn.is_closed():
            return _ssh_conn
        _ssh_conn = await asyncssh.connect(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,  # local trusted host, no host-key pinning needed
        )
        return _ssh_conn


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shell",
    instructions=(
        "Real SSH terminal access to the Home Assistant host (same as the "
        "Advanced SSH & Web Terminal add-on). Use execute_command to run any "
        "bash command, including `ha`, `docker`, and access to /addons, "
        "/share, /config and the rest of the host filesystem. "
        "Working directory defaults to /config."
    ),
)

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def execute_command(
    cmd: str,
    workdir: str = "/config",
    timeout: int = 60,
) -> dict:
    """
    Execute a shell command on the Home Assistant host via SSH — identical
    to running it yourself in the Advanced SSH & Web Terminal add-on.

    Args:
        cmd:     The bash command to run (e.g. 'ha apps reload', 'docker ps')
        workdir: Working directory (default: /config)
        timeout: Max seconds to wait for completion (default: 60, max: 300)
    """
    timeout = min(timeout, 300)
    full_cmd = f"cd {workdir!r} && {cmd}"

    try:
        conn = await _get_connection()
        try:
            result = await asyncio.wait_for(
                conn.run(full_cmd, check=False), timeout=timeout
            )
        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"Command timed out after {timeout}s",
                "cmd": cmd,
            }

        return {
            "success": result.exit_status == 0,
            "returncode": result.exit_status,
            "cmd": cmd,
            "workdir": workdir,
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
        }

    except (asyncssh.Error, OSError) as e:
        # Connection likely stale/dropped — force reconnect on next call
        global _ssh_conn
        async with _ssh_lock:
            _ssh_conn = None
        return {
            "success": False,
            "error": f"SSH error: {e}",
            "cmd": cmd,
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "cmd": cmd,
        }


# ── Token auth middleware (protects this MCP server from clients) ────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not TOKEN:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        token_param = request.query_params.get("token", "")
        if auth_header == f"Bearer {TOKEN}" or token_param == TOKEN:
            return await call_next(request)
        return Response("Unauthorized", status_code=401)


# ── App assembly ──────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8767)
