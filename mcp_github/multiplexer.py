#!/usr/bin/env python3
"""
MCP GitHub Multiplexer
Proxies all tools from the official github-mcp-server and adds binary file support.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer


UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8767"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8766"))
GITHUB_TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
UPSTREAM_URL = f"http://127.0.0.1:{UPSTREAM_PORT}/mcp"

BINARY_TOOLS = [
    {
        "name": "push_binary_file",
        "description": "Push a binary file (e.g. PNG image) to a GitHub repository using base64-encoded content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "path": {"type": "string", "description": "File path in the repository"},
                "content_base64": {"type": "string", "description": "Base64-encoded file content"},
                "message": {"type": "string", "description": "Commit message"},
                "branch": {"type": "string", "description": "Branch name (default: main)"},
            },
            "required": ["owner", "repo", "path", "content_base64", "message"],
        },
    },
    {
        "name": "get_binary_file",
        "description": "Get a binary file from a GitHub repository. Returns base64-encoded content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "path": {"type": "string", "description": "File path in the repository"},
            },
            "required": ["owner", "repo", "path"],
        },
    },
]

# Required headers for MCP Streamable HTTP protocol
def _upstream_headers(session_id: str | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {GITHUB_TOKEN}",
    }
    if session_id:
        h["Mcp-Session-Id"] = session_id
    return h


def _github_api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, method=method,
        headers={
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}"}


def handle_push_binary_file(args: dict) -> str:
    owner = args["owner"]
    repo = args["repo"]
    path = args["path"]
    content_b64 = args["content_base64"]
    message = args["message"]
    branch = args.get("branch", "main")

    existing = _github_api("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
    sha = existing.get("sha")

    body = {"message": message, "content": content_b64, "branch": branch}
    if sha:
        body["sha"] = sha

    result = _github_api("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)
    if "error" in result:
        return f"Error: {result['error']}"
    action = "Updated" if sha else "Created"
    html_url = result.get("content", {}).get("html_url", "")
    return f"{action} {path} in {owner}/{repo} - {html_url}"


def handle_get_binary_file(args: dict) -> str:
    owner = args["owner"]
    repo = args["repo"]
    path = args["path"]

    result = _github_api("GET", f"/repos/{owner}/{repo}/contents/{path}")
    if "error" in result:
        return f"Error: {result['error']}"

    content_b64 = result.get("content", "").replace("\n", "")
    size = result.get("size", 0)
    return json.dumps({"path": path, "size": size, "encoding": "base64", "content_base64": content_b64})


def _wait_for_upstream(retries: int = 15, delay: float = 4.0) -> bool:
    print(f"Waiting for upstream on port {UPSTREAM_PORT} (max {int(retries * delay)}s)...", flush=True)
    for i in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                UPSTREAM_URL, method="POST",
                headers=_upstream_headers(),
                data=json.dumps({
                    "jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "probe", "version": "0"}}
                }).encode(),
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                print(f"Upstream ready (HTTP {resp.status}) after attempt {i}/{retries}", flush=True)
                return True
        except urllib.error.HTTPError as e:
            # Any response means the server is up
            print(f"Upstream ready (HTTP {e.code}) after attempt {i}/{retries}", flush=True)
            return True
        except Exception as e:
            print(f"Attempt {i}/{retries}: {e}, retrying in {delay:.0f}s...", flush=True)
            time.sleep(delay)
    return False


def _forward_to_upstream(body: bytes, session_id: str | None) -> tuple[int, dict, str]:
    req = urllib.request.Request(
        UPSTREAM_URL, method="POST",
        headers=_upstream_headers(session_id),
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, dict(resp.headers), resp.read().decode()
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Upstream requires fresh session — reinitialize and retry
            new_session = _upstream_initialize()
            if new_session:
                req2 = urllib.request.Request(
                    UPSTREAM_URL, method="POST",
                    headers=_upstream_headers(new_session),
                    data=body,
                )
                try:
                    with urllib.request.urlopen(req2, timeout=60) as resp2:
                        return resp2.status, dict(resp2.headers), resp2.read().decode()
                except urllib.error.HTTPError as e2:
                    return e2.code, {}, e2.read().decode()
        return e.code, {}, e.read().decode()
    except Exception as e:
        return 502, {}, json.dumps({"error": str(e)})


def _make_tool_result(req_id, content: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": req_id,
        "result": {"content": [{"type": "text", "text": content}], "isError": False},
    })


def _upstream_initialize() -> str | None:
    """Do MCP initialize handshake and return session_id."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "multiplexer", "version": "1"}}
    }).encode()
    req = urllib.request.Request(
        UPSTREAM_URL, method="POST",
        headers=_upstream_headers(),
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            session_id = resp.headers.get("Mcp-Session-Id")
            return session_id
    except Exception as e:
        print(f"[MUX] initialize failed: {e}", flush=True)
        return None


def _parse_sse_or_json(text: str) -> dict:
    """Parse either plain JSON or SSE event stream response."""
    text = text.strip()
    if text.startswith("{"):
        return json.loads(text)
    # SSE format: lines like "data: {...}"
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return {}


def _get_upstream_tools(session_id: str | None) -> list:
    # If no session_id provided, create a fresh one
    if not session_id:
        session_id = _upstream_initialize()
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    _, _, resp_text = _forward_to_upstream(body, session_id)
    try:
        return _parse_sse_or_json(resp_text).get("result", {}).get("tools", [])
    except Exception as e:
        print(f"[MUX] tools/list parse error: {e}", flush=True)
        return []


class MuxHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[MUX] {fmt % args}", flush=True)

    def _send_json(self, status: int, body: str, extra_headers: dict | None = None):
        encoded = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Mcp-Session-Id, Authorization")
        self.end_headers()

    def do_GET(self):
        status, headers, body = _forward_to_upstream(b"", self.headers.get("Mcp-Session-Id"))
        self._send_json(status, body)

    def do_DELETE(self):
        status, headers, body = _forward_to_upstream(b"", self.headers.get("Mcp-Session-Id"))
        self._send_json(status, body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        session_id = self.headers.get("Mcp-Session-Id")
        # If no session from client, create one internally
        if not session_id:
            session_id = _upstream_initialize()

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, json.dumps({"error": "invalid json"}))
            return

        method = msg.get("method", "")
        req_id = msg.get("id")

        if method == "tools/list":
            upstream_tools = _get_upstream_tools(session_id)
            resp = json.dumps({"jsonrpc": "2.0", "id": req_id,
                               "result": {"tools": upstream_tools + BINARY_TOOLS}})
            self._send_json(200, resp)
            return

        if method == "tools/call":
            tool_name = msg.get("params", {}).get("name", "")
            tool_args = msg.get("params", {}).get("arguments", {})

            if tool_name == "push_binary_file":
                self._send_json(200, _make_tool_result(req_id, handle_push_binary_file(tool_args)))
                return
            if tool_name == "get_binary_file":
                self._send_json(200, _make_tool_result(req_id, handle_get_binary_file(tool_args)))
                return

        status, resp_headers, body = _forward_to_upstream(raw, session_id)
        extra = {}
        if "Mcp-Session-Id" in resp_headers:
            extra["Mcp-Session-Id"] = resp_headers["Mcp-Session-Id"]
        self._send_json(status, body, extra)


if __name__ == "__main__":
    if not _wait_for_upstream():
        print("ERROR: Upstream not reachable after 60s. Binary tools will work but GitHub API tools will not.", flush=True)
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), MuxHandler)
    print(f"MCP GitHub Multiplexer listening on port {LISTEN_PORT}", flush=True)
    server.serve_forever()
