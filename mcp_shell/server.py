"""
MCP Shell Server for Home Assistant v2.3.0
Provides execute_command tool for running shell commands via SSH on the real
Home Assistant host (Advanced SSH & Web Terminal add-on).

Log format per Befehl:
  Anfrage: <workdir> $ <cmd>
  Antwort: OK (rc=0) | <stdout-preview>
  Antwort: FEHLER (rc=N) | <stdout> | <stderr>
"""

import asyncio
import logging
import os

import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_shell")

# Unterdrücke ganzen Framework-/HTTP-Spam
for _noisy in (
    "uvicorn.access",
    "uvicorn.error",
    "asyncssh",
    "mcp",           # FastMCP "Processing request of type ..." noise
    "mcp.server",
    "mcp.server.fastmcp",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Config from environment ───────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")
SSH_HOST = os.environ.get("SSH_HOST", "127.0.0.1")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/data/ssh_key/mcp_shell_key")

log.info("Token auth: %s", "enabled" if TOKEN else "disabled")
log.info("SSH target: %s@%s:%s", SSH_USER, SSH_HOST, SSH_PORT)

# ── SSH connection (persistent, auto-reconnect) ──────────────────────────────

_ssh_conn = None
_ssh_lock = asyncio.Lock()


async def _get_connection():
    global _ssh_conn
    async with _ssh_lock:
        if _ssh_conn is not None and not _ssh_conn.is_closed():
            return _ssh_conn
        log.warning("SSH: Verbindungsaufbau zu %s@%s:%s ...", SSH_USER, SSH_HOST, SSH_PORT)
        _ssh_conn = await asyncssh.connect(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,
        )
        log.info("SSH: Verbunden")
        return _ssh_conn


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shell",
    instructions=(
        "Real SSH terminal access to the Home Assistant host. "
        "Use execute_command to run any bash command including `ha`, `docker`. "
        "Working directory defaults to /homeassistant (same as /config). "
        "SUPERVISOR_TOKEN is exported automatically."
    ),
)

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Helper ──────────────────────────────────────────────────────────────────────

def _preview(text: str, max_chars: int = 300) -> str:
    """Erste max_chars Zeichen, mehrzeilig erhalten, mit Ellipse wenn gekürzt."""
    t = text.strip()
    if not t:
        return "(leer)"
    if len(t) <= max_chars:
        return t
    return t[:max_chars] + " …"


# ── Tool ───────────────────────────────────────────────────────────────────────

@mcp.tool()
async def execute_command(
    cmd: str,
    workdir: str = "/homeassistant",
    timeout: int = 60,
) -> dict:
    """
    Execute a shell command on the Home Assistant host via SSH.

    Args:
        cmd:     The bash command to run (e.g. 'ha apps reload', 'docker ps')
        workdir: Working directory (default: /homeassistant)
        timeout: Max seconds to wait for completion (default: 60, max: 300)
    """
    timeout = min(timeout, 300)

    log.info("Anfrage: %s $ %s", workdir, cmd)

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
            log.error("Antwort: TIMEOUT nach %ss", timeout)
            return {"success": False, "error": f"Command timed out after {timeout}s", "cmd": cmd}

        stdout = result.stdout or ""
        stderr = result.stderr or ""
        rc = result.exit_status

        if rc == 0:
            out_preview = _preview(stdout)
            if stderr.strip():
                log.info("Antwort: OK (rc=0)\n  stdout: %s\n  stderr: %s", out_preview, _preview(stderr))
            else:
                log.info("Antwort: OK (rc=0)\n  stdout: %s", out_preview)
        else:
            log.warning(
                "Antwort: FEHLER (rc=%s)\n  stdout: %s\n  stderr: %s",
                rc, _preview(stdout), _preview(stderr),
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
        log.error("Antwort: SSH-Fehler: %s", e)
        return {"success": False, "error": f"SSH error: {e}", "cmd": cmd}
    except Exception as e:
        log.error("Antwort: Unerwarteter Fehler: %s", e)
        return {"success": False, "error": str(e), "cmd": cmd}


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
    uvicorn.run(app, host="0.0.0.0", port=8767, log_level="warning")
