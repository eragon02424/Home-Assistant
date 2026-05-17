"""
MCP File Server for Home Assistant v1.3.0
Provides read/write access to host filesystem paths defined in a whitelist.
"""

import os
import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Config from environment ──────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")

_raw_paths = os.environ.get("MCP_ALLOWED_PATHS", "/config")
ALLOWED_PATHS = [p.strip().rstrip("/") for p in _raw_paths.split(",") if p.strip()]

print(f"[MCP File Server] Allowed paths: {ALLOWED_PATHS}")
print(f"[MCP File Server] Token auth: {'enabled' if TOKEN else 'disabled'}")

# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP File Server",
    instructions=(
        "File system access for Home Assistant host. "
        "Allowed paths are configured via the add-on whitelist. "
        "Always use absolute paths starting with /config, /media, etc."
    ),
)

# DNS Rebinding Protection deaktivieren damit externe Hosts akzeptiert werden
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Path security helpers ─────────────────────────────────────────────────────

def _resolve(path: str) -> Path:
    return Path(path).resolve()

def _is_allowed(path: str) -> bool:
    resolved = _resolve(path)
    for allowed in ALLOWED_PATHS:
        try:
            resolved.relative_to(_resolve(allowed))
            return True
        except ValueError:
            continue
    return False

def _check(path: str) -> Path:
    if not _is_allowed(path):
        raise PermissionError(
            f"Path '{path}' is not in the allowed paths whitelist: {ALLOWED_PATHS}"
        )
    return _resolve(path)

# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_files(path: str, pattern: str = "*") -> dict:
    """List files and directories. Args: path (absolute, must be in whitelist), pattern (glob filter, default *)"""
    resolved = _check(path)
    if not resolved.exists():
        return {"success": False, "error": f"Path does not exist: {path}"}
    if not resolved.is_dir():
        return {"success": False, "error": f"Path is not a directory: {path}"}
    entries = []
    for item in sorted(resolved.glob(pattern)):
        entries.append({
            "name": item.name,
            "path": str(item),
            "type": "directory" if item.is_dir() else "file",
            "size": item.stat().st_size if item.is_file() else None,
        })
    return {"success": True, "path": str(resolved), "entries": entries}


@mcp.tool()
def read_file(path: str) -> dict:
    """Read file contents. Args: path (absolute, must be in whitelist)"""
    resolved = _check(path)
    if not resolved.exists():
        return {"success": False, "error": f"File does not exist: {path}"}
    if not resolved.is_file():
        return {"success": False, "error": f"Path is not a file: {path}"}
    try:
        content = resolved.read_text(encoding="utf-8")
        return {"success": True, "path": str(resolved), "content": content}
    except UnicodeDecodeError:
        import base64
        content_b64 = base64.b64encode(resolved.read_bytes()).decode()
        return {"success": True, "path": str(resolved), "content": content_b64, "encoding": "base64"}


@mcp.tool()
def write_file(path: str, content: str, overwrite: bool = False) -> dict:
    """Write content to file. Creates parent dirs if needed. Args: path, content, overwrite (default false)"""
    resolved = _check(path)
    if resolved.exists() and not overwrite:
        return {"success": False, "error": "File already exists. Set overwrite=true to replace it."}
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(resolved), "bytes_written": len(content.encode())}


@mcp.tool()
def delete_file(path: str) -> dict:
    """Delete a file (not directory). Args: path (absolute, must be in whitelist)"""
    resolved = _check(path)
    if not resolved.exists():
        return {"success": False, "error": f"File does not exist: {path}"}
    if resolved.is_dir():
        return {"success": False, "error": "Use delete_directory to remove directories."}
    resolved.unlink()
    return {"success": True, "path": str(resolved)}


@mcp.tool()
def delete_directory(path: str, recursive: bool = False) -> dict:
    """Delete a directory. Args: path, recursive (default false)"""
    resolved = _check(path)
    if not resolved.exists():
        return {"success": False, "error": f"Directory does not exist: {path}"}
    if not resolved.is_dir():
        return {"success": False, "error": "Path is not a directory."}
    if recursive:
        shutil.rmtree(resolved)
    else:
        try:
            resolved.rmdir()
        except OSError:
            return {"success": False, "error": "Directory is not empty. Set recursive=true to force delete."}
    return {"success": True, "path": str(resolved)}


@mcp.tool()
def move_file(source: str, destination: str, overwrite: bool = False) -> dict:
    """Move or rename file/directory. Args: source, destination, overwrite (default false)"""
    src = _check(source)
    dst = _check(destination)
    if not src.exists():
        return {"success": False, "error": f"Source does not exist: {source}"}
    if dst.exists() and not overwrite:
        return {"success": False, "error": "Destination already exists. Set overwrite=true."}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"success": True, "source": str(src), "destination": str(dst)}


@mcp.tool()
def get_allowed_paths() -> dict:
    """Return the whitelisted paths this server can access."""
    return {"allowed_paths": ALLOWED_PATHS}


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
    uvicorn.run(app, host="0.0.0.0", port=8765)
