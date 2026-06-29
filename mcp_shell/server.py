"""
MCP Shell Server for Home Assistant v1.0.0
Provides execute_command tool for running arbitrary shell commands.
Authentication via Bearer token (same pattern as mcp_file_server).
"""

import asyncio
import os

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Config from environment ───────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")

print(f"[MCP Shell] Token auth: {'enabled' if TOKEN else 'disabled'}")

# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Shell",
    instructions=(
        "Shell access for Home Assistant host. "
        "Use execute_command to run any bash command. "
        "stdout and stderr are returned separately. "
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
    Execute a shell command on the Home Assistant host.

    Args:
        cmd:     The bash command to run (e.g. 'esphome logs mydevice.yaml')
        workdir: Working directory (default: /config)
        timeout: Max seconds to wait for completion (default: 60, max: 300)
    """
    timeout = min(timeout, 300)

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "success": False,
                "error": f"Command timed out after {timeout}s",
                "cmd": cmd,
            }

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        returncode = proc.returncode

        return {
            "success": returncode == 0,
            "returncode": returncode,
            "cmd": cmd,
            "workdir": workdir,
            "stdout": stdout,
            "stderr": stderr,
        }

    except Exception as e:
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
        return Response("Unauthorized", status_code=401)


# ── App assembly ──────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8767)
