#!/usr/bin/env python3
"""
Minimal reverse proxy that strips the Origin header
so mcp-go's origin check is skipped entirely.
"""
import asyncio
import aiohttp
from aiohttp import web

TARGET = "http://127.0.0.1:8081"


async def proxy(request: web.Request) -> web.StreamResponse:
    # Build forwarded headers, removing Origin and Host (will be set by aiohttp)
    skip = {"host", "origin", "transfer-encoding", "connection",
            "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailers", "upgrade"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    url = TARGET + request.path_qs
    body = await request.read()

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=request.method,
            url=url,
            headers=headers,
            data=body if body else None,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            response = web.StreamResponse(status=resp.status, headers=dict(resp.headers))
            await response.prepare(request)
            async for chunk in resp.content.iter_chunked(8192):
                await response.write(chunk)
            await response.write_eof()
            return response


app = web.Application()
app.router.add_route("*", "/{path_info:.*}", proxy)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None)
