#!/usr/bin/env python3
"""
MCP GitHub Multiplexer
Combines the official github-mcp-server with custom binary file tools.
Listens on port 8766, forwards known tools to upstream on port 8767,
handles push_file_binary and get_file_binary locally.
"""

import asyncio
import base64
import json
import logging
import os
import urllib.request
import urllib.error
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http import streamable_http_app
import mcp.types as types
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", "8767"))
GITHUB_TOKEN = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8766"))


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _github_api(method: str, path: str, body: dict | None = None) -> dict:
    url = f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        method=method,
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
        body_text = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {path}: HTTP {e.code} \u2014 {body_text}")


def _get_file_sha(owner: str, repo: str, path: str, branch: str) -> str | None:
    try:
        result = _github_api("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")
        return result.get("sha")
    except RuntimeError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Upstream forwarding
# ─────────────────────────────────────────────────────────────────────────────

async def _upstream_initialize() -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "multiplexer", "version": "1.0"},
        },
    }
    return await _upstream_request(payload)


async def _upstream_list_tools() -> list[dict]:
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    result = await _upstream_request(payload)
    return result.get("result", {}).get("tools", [])


async def _upstream_call_tool(name: str, arguments: dict) -> list[dict]:
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    result = await _upstream_request(payload)
    return result.get("result", {}).get("content", [{"type": "text", "text": str(result)}])


async def _upstream_request(payload: dict) -> dict:
    url = f"http://127.0.0.1:{UPSTREAM_PORT}/mcp"
    data = json.dumps(payload).encode()
    loop = asyncio.get_event_loop()

    def _do_request():
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            data=data,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
                return json.loads(raw)
        except Exception as e:
            return {"error": str(e)}

    return await loop.run_in_executor(None, _do_request)


# ─────────────────────────────────────────────────────────────────────────────
# MCP Server
# ─────────────────────────────────────────────────────────────────────────────

async def build_server() -> Server:
    server = Server("mcp-github-extended")

    for attempt in range(20):
        try:
            await _upstream_initialize()
            log.info("Upstream github-mcp-server is ready")
            break
        except Exception:
            if attempt == 19:
                log.warning("Upstream not ready after 20 attempts, continuing anyway")
            await asyncio.sleep(1)

    upstream_tools = await _upstream_list_tools()
    upstream_tool_names = {t["name"] for t in upstream_tools}
    log.info(f"Upstream has {len(upstream_tools)} tools")

    custom_tools = [
        types.Tool(
            name="push_file_binary",
            description="Push a binary or text file to a GitHub repository using base64-encoded content. Use this for images (PNG, JPG), icons, or any non-text file.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "path": {"type": "string", "description": "File path in the repository"},
                    "content_base64": {"type": "string", "description": "Base64-encoded file content"},
                    "message": {"type": "string", "description": "Commit message"},
                    "branch": {"type": "string", "description": "Branch name", "default": "main"},
                },
                "required": ["owner", "repo", "path", "content_base64", "message"],
            },
        ),
        types.Tool(
            name="get_file_binary",
            description="Get a binary file from a GitHub repository as base64-encoded content. Use for images or other binary files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner"},
                    "repo": {"type": "string", "description": "Repository name"},
                    "path": {"type": "string", "description": "File path in the repository"},
                    "branch": {"type": "string", "description": "Branch name", "default": "main"},
                },
                "required": ["owner", "repo", "path"],
            },
        ),
    ]

    all_tools = list(custom_tools)
    for t in upstream_tools:
        all_tools.append(types.Tool(
            name=t["name"],
            description=t.get("description", ""),
            inputSchema=t.get("inputSchema", {"type": "object", "properties": {}}),
        ))

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return all_tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:

        if name == "push_file_binary":
            owner = arguments["owner"]
            repo = arguments["repo"]
            path = arguments["path"]
            content_b64 = arguments["content_base64"]
            message = arguments["message"]
            branch = arguments.get("branch", "main")

            sha = await asyncio.get_event_loop().run_in_executor(
                None, _get_file_sha, owner, repo, path, branch
            )
            body: dict = {"message": message, "content": content_b64, "branch": branch}
            if sha:
                body["sha"] = sha

            def _push():
                return _github_api("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)

            try:
                result = await asyncio.get_event_loop().run_in_executor(None, _push)
                commit_sha = result.get("commit", {}).get("sha", "unknown")
                return [types.TextContent(type="text", text=f"File pushed successfully. Commit: {commit_sha}")]
            except RuntimeError as e:
                return [types.TextContent(type="text", text=f"Error: {e}")]

        if name == "get_file_binary":
            owner = arguments["owner"]
            repo = arguments["repo"]
            path = arguments["path"]
            branch = arguments.get("branch", "main")

            def _get():
                return _github_api("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}")

            try:
                result = await asyncio.get_event_loop().run_in_executor(None, _get)
                content_b64 = result.get("content", "").replace("\n", "")
                size = result.get("size", 0)
                return [types.TextContent(type="text", text=f"File retrieved. Size: {size} bytes. Base64:\n{content_b64}")]
            except RuntimeError as e:
                return [types.TextContent(type="text", text=f"Error: {e}")]

        if name in upstream_tool_names:
            content = await _upstream_call_tool(name, arguments)
            return [types.TextContent(type="text", text=str(c.get("text", c))) for c in content]

        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    return server


async def main():
    server = await build_server()
    app = streamable_http_app(server)
    config = uvicorn.Config(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")
    uvi_server = uvicorn.Server(config)
    log.info(f"MCP GitHub Extended listening on port {LISTEN_PORT}")
    await uvi_server.serve()


if __name__ == "__main__":
    asyncio.run(main())
