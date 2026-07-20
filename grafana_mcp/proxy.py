#!/usr/bin/env python3
"""
Minimal reverse proxy that rewrites Host and Origin headers
so mcp-go's allowlist check passes for 127.0.0.1:8081.
"""
import asyncio
import aiohttp
from aiohttp import web

TARGET = "http://127.0.0.1:8081"
FORCED_HOST = "127.0.0.1:8081"
FORCED_ORIGIN = "http://127.0.0.1:8081"


async def proxy(request: web.Request) -> web.StreamResponse:
    # Build forwarded headers, overriding Host and Origin
    headers = dict(request.headers)
    headers["Host"] = FORCED_HOST
    headers["Origin"] = FORCED_ORIGIN
    # Remove hop-by-hop headers
    for h in ("Transfer-Encoding", "Connection", "Keep-Alive", "Proxy-Authenticate",
               "Proxy-Authorization", "TE", "Trailers", "Upgrade"):
        headers.pop(h, None)

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
            # Stream response back
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
