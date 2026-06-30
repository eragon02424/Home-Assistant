"""
MCP Shell Server for Home Assistant v2.2.0
Provides execute_command tool for running shell commands via SSH on the real
Home Assistant host (Advanced SSH & Web Terminal add-on), giving access to
the `ha` CLI, `docker`, and the full host filesystem — exactly as if the
user typed the command themselves over SSH.

Authentication to clients: Bearer token (same pattern as before).
Authentication to the HA host: SSH key-based auth (no password used).

Logging:
  INFO  — every command sent + result summary (exit code, first line of output)
  WARNING — SSH reconnects, auth failures
  ERROR — exceptions, timeouts, connection failures
  (uvicorn HTTP noise is suppressed to WARNING level)
"""

import asyncio
import logging
import os
import textwrap

import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Logging setup ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_shell")

# Suppress uvicorn access log spam (every HTTP request) — keep uvicorn errors
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("asyncssh").setLevel(logging.WARNING)

# ── Config from environment ───────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")

SSH_HOST = os.environ.get("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/data/ssh_key/mcp_shell_key")

log.info("Token auth: %s", "enabled" if TOKEN else "disabled")
log.info("SSH target: %s@%s:%s", SSH_USER, SSH_HOST, SSH_PORT)

# ── Persistent SSH connection (reconnect on failure) ──────────────────────────

_ssh_conn = None
_ssh_lock = asyncio.Lock()


async def _get_connection():
    global _ssh_conn
    async with _ssh_lock:
        if _ssh_conn is not None and not _ssh_conn.is_closed():
            return _ssh_conn
        log.warning("SSH: (re)connecting to %s@%s:%s", SSH_USER, SSH_HOST, SSH_PORT)
        _ssh_conn = await asyncssh.connect(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,
        )
        log.info("SSH: connected")
        return _ssh_conn


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shell",
    instructions=(
        "Real SSH terminal access to the Home Assistant host (same as the "
        "Advanced SSH & Web Terminal add-on). Use execute_command to run any "
        "bash command, including `ha`, `docker`, and access to /addons, "
        "/share, /homeassistant and the rest of the host filesystem. "
        "Working directory defaults to /homeassistant (= /config). "
        "SUPERVISOR_TOKEN is exported automatically so `ha` CLI commands "
        "work without extra setup."
    ),
)

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Tools ─────────────────────────────────────────────────────────────────────

def _truncate(text: str, max_chars: int = 200) -> str:
    """Return first line(s) up to max_chars for log output."""
    preview = text.strip()[:max_chars]
    if len(text.strip()) > max_chars:
        preview += " …"
    return preview


@mcp.tool()
async def execute_command(
    cmd: str,
    workdir: str = "/homeassistant",
    timeout: int = 60,
) -> dict:
    """
    Execute a shell command on the Home Assistant host via SSH — identical
    to running it yourself in the Advanced SSH & Web Terminal add-on.

    Args:
        cmd:     The bash command to run (e.g. 'ha apps reload', 'docker ps')
        workdir: Working directory (default: /homeassistant)
        timeout: Max seconds to wait for completion (default: 60, max: 300)
    """
    timeout = min(timeout, 300)

    log.info("CMD  [%s] %s", workdir, cmd)

    full_cmd = (
        f"cd {workdir!r} && "
        f"export SUPERVISOR_TOKEN=$(grep -h SUPERVISOR_TOKEN /etc/profile.d/*.sh 2>/dev/null "
        f"| head -1 | cut -d'\"' -f2); {cmd}"
    )

    try:
        conn = await _get_connection()
        try:
            result = await asyncio.wait_for(
                conn.run(full_cmd, check=False), timeout=timeout
            )
        except asyncio.TimeoutError:
            log.error("TIMEOUT after %ss | CMD: %s", timeout, cmd)
            return {
                "success": False,
                "error": f"Command timed out after {timeout}s",
                "cmd": cmd,
            }

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        rc = result.exit_status

        if rc == 0:
            log.info(
                "OK   rc=0 | stdout: %s%s",
                _truncate(stdout),
                f" | stderr: {_truncate(stderr)}" if stderr.strip() else "",
            )
        else:
            log.warning(
                "FAIL rc=%s | stdout: %s | stderr: %s",
                rc,
                _truncate(stdout),
                _truncate(stderr),
            )

        return {
            "success": rc == 0,
            "returncode": rc,
            "cmd": cmd,
            "workdir": workdir,
            "stdout": stdout,
            "stderr": stderr,
        }

    except (asyncssh.Error, OSError) as e:
        global _ssh_conn
        async with _ssh_lock:
            _ssh_conn = None
        log.error("SSH error: %s | CMD: %s", e, cmd)
        return {
            "success": False,
            "error": f"SSH error: {e}",
            "cmd": cmd,
        }
    except Exception as e:
        log.error("Unexpected error: %s | CMD: %s", e, cmd)
        return {
            "success": False,
            "error": str(e),
            "cmd": cmd,
        }


# ── Token auth middleware ─────────────────────────────────────────────────────

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


# ── App assembly ──────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8767,
        log_level="warning",  # suppress uvicorn access log noise
    )
