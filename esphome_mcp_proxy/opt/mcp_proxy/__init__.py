"""MCP Proxy — routes MCP requests to multiple MCP server addons via webhooks.

Each active server slot gets its own OAuth-protected webhook endpoint.
Configuration is read from /config/.mcp_proxy_config.json written by the addon.

Enable debug logging in configuration.yaml:
  logger:
    logs:
      custom_components.mcp_proxy: debug
"""

import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.components.webhook import async_register, async_unregister
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.typing import ConfigType

_LOGGER = logging.getLogger(__name__)

DOMAIN = "mcp_proxy"
CONFIG_FILE = Path("/config/.mcp_proxy_config.json")

_HOP_BY_HOP = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "cookie", "authorization",
})
_SENSITIVE_HEADER_NAMES = frozenset({
    "authorization", "x-ha-access", "x-supervisor-token",
})
_LOG_BODY_MAX = 4096


# ─────────────────────────────────────────────────────────────────────────────
# Logging helpers
# ─────────────────────────────────────────────────────────────────────────────

def _mask_sensitive_headers(headers) -> dict:
    result = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_NAMES:
            result[k] = "Bearer ***" if v.lower().startswith("bearer ") else "***"
        else:
            result[k] = v
    return result


def _truncate(data, max_len: int = _LOG_BODY_MAX) -> str:
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            text = repr(data)
    else:
        text = data
    if len(text) > max_len:
        return text[:max_len] + f"... [TRUNCATED, total {len(text)} chars]"
    return text


def _mask_url(url: str) -> str:
    masked = re.sub(r"(/api/webhook/)[^/?#]+", r"\1***", url)
    masked = re.sub(r"(/private_)[^/?#]+", r"\1***", masked)
    return masked


def _validate_target_url(target_url: str) -> tuple[bool, str]:
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        return False, f"scheme must be http or https, got {parsed.scheme!r}"
    if not parsed.netloc:
        return False, "URL is missing host"
    return True, ""


# ─────────────────────────────────────────────────────────────────────────────
# HA setup
# ─────────────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    if DOMAIN in config:
        _LOGGER.info("MCP Proxy: Found YAML config — migrating to config entry.")
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": "import"})
        )
    if await hass.async_add_executor_job(_marker_present):
        from .repairs import maybe_create_issue
        maybe_create_issue(hass, DOMAIN)
    return True


def _marker_present() -> bool:
    from .repairs import marker_present
    return marker_present()


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("MCP Proxy: async_setup_entry called")

    try:
        proxy_config = await hass.async_add_executor_job(_read_config)
    except (OSError, json.JSONDecodeError) as err:
        _LOGGER.error("MCP Proxy: Failed to read %s: %s", CONFIG_FILE, err)
        raise ConfigEntryError(f"Failed to read {CONFIG_FILE}: {err}") from err

    if proxy_config is None:
        _LOGGER.info("MCP Proxy: No config found at %s. Start the MCP Proxy addon to activate.", CONFIG_FILE)
        return True

    servers = proxy_config.get("servers", [])
    if not servers:
        _LOGGER.error("MCP Proxy: No servers in config file. Restart the addon.")
        raise ConfigEntryError("No servers configured in .mcp_proxy_config.json")

    public_base_url = proxy_config.get("public_base_url")
    if not isinstance(public_base_url, str) or not public_base_url:
        _LOGGER.warning("MCP Proxy: no public_base_url set — OAuth URLs built from request headers")
        public_base_url = None

    # Shared aiohttp session for all upstream requests
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=300, sock_connect=10, sock_read=300),
    )

    slot_data = {}

    for server in servers:
        slot = server.get("slot", "?")
        target_url = server.get("target_url", "")
        webhook_id = server.get("webhook_id", "")
        token = server.get("token", "")
        oauth_section = server.get("oauth", {})

        if not target_url or not webhook_id:
            _LOGGER.error("MCP Proxy: Slot %s missing target_url or webhook_id — skipping", slot)
            continue

        is_valid, reason = _validate_target_url(target_url)
        if not is_valid:
            _LOGGER.error("MCP Proxy: Slot %s invalid target_url: %s — skipping", slot, reason)
            continue

        masked_target = _mask_url(target_url)
        masked_wh = webhook_id[:6] + "..." if len(webhook_id) > 6 else "***"
        _LOGGER.info("MCP Proxy: Slot %s target = %s, webhook = /api/webhook/%s", slot, masked_target, masked_wh)

        client_id = str(oauth_section.get("client_id", ""))
        client_secret = str(oauth_section.get("client_secret", ""))
        if not client_id or not client_secret:
            _LOGGER.error("MCP Proxy: Slot %s missing OAuth credentials — skipping", slot)
            continue

        # Register webhook for this slot
        webhook_name = f"MCP Proxy Slot {slot}"
        try:
            async_register(
                hass, DOMAIN, webhook_name, webhook_id,
                _make_webhook_handler(slot),
                allowed_methods=["POST", "GET"],
            )
            _LOGGER.info("MCP Proxy: Slot %s webhook registered", slot)
        except Exception as err:
            _LOGGER.exception("MCP Proxy: Slot %s failed to register webhook: %s", slot, err)
            continue

        # OAuth provider for this slot
        from .oauth import OAuthProvider, load_or_create_secret
        try:
            signing_key = await hass.async_add_executor_job(load_or_create_secret)
            oauth_provider = OAuthProvider(
                hass=hass,
                client_id=client_id,
                client_secret=client_secret,
                webhook_id=webhook_id,
                signing_key=signing_key,
                public_base_url=public_base_url,
                slot=slot,
            )
            oauth_provider.register_views()
            _LOGGER.info("MCP Proxy: Slot %s OAuth ENABLED (client_id=%s)", slot, oauth_provider.client_id_masked())
        except Exception as err:
            _LOGGER.exception("MCP Proxy: Slot %s failed to init OAuth: %s", slot, err)
            async_unregister(hass, webhook_id)
            continue

        slot_data[str(slot)] = {
            "slot": slot,
            "target_url": target_url,
            "webhook_id": webhook_id,
            "token": token,
            "oauth": oauth_provider,
            "session": session,
        }

    if not slot_data:
        await session.close()
        raise ConfigEntryError("No server slots could be set up. Check HA logs for mcp_proxy errors.")

    hass.data[DOMAIN] = {"slots": slot_data, "session": session}

    from .repairs import _clear_marker, _delete_issue_only
    await hass.async_add_executor_job(_clear_marker)
    _delete_issue_only(hass, DOMAIN)

    _LOGGER.info("MCP Proxy: setup complete — %d slot(s) active", len(slot_data))
    return True


def _read_config() -> dict | None:
    if not CONFIG_FILE.exists():
        return None
    return json.loads(CONFIG_FILE.read_text())


def _make_webhook_handler(slot):
    """Return a webhook handler bound to the given slot number."""
    async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.StreamResponse:
        data = hass.data[DOMAIN]["slots"].get(str(slot))
        if not data:
            return web.Response(status=503, text=f"MCP Proxy: slot {slot} not configured")
        return await _proxy_request(data, request)
    return _handle_webhook


async def _proxy_request(data: dict, request: web.Request) -> web.StreamResponse:
    target_url = data["target_url"]
    oauth_provider = data["oauth"]
    session = data["session"]
    slot = data["slot"]

    req_id = f"{slot}:{int(time.monotonic() * 1000) % 100000:05d}"
    masked_target = _mask_url(target_url)

    _LOGGER.debug("[%s] [REQ] %s %s (remote=%s)", req_id, request.method, _mask_url(str(request.url)), request.remote)
    _LOGGER.debug("[%s] [REQ-HEADERS] %s", req_id, _mask_sensitive_headers(dict(request.headers)))

    body = await request.read()
    if body:
        _LOGGER.debug("[%s] [REQ-BODY] %d bytes: %s", req_id, len(body), _truncate(body))
    else:
        _LOGGER.debug("[%s] [REQ-BODY] <empty>", req_id)

    # OAuth gate
    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        _LOGGER.info("[%s] [AUTH] No Authorization header — returning 401", req_id)
    elif not auth_header.lower().startswith("bearer "):
        _LOGGER.info("[%s] [AUTH] Non-Bearer Authorization — returning 401", req_id)
    else:
        _LOGGER.debug("[%s] [AUTH] Bearer token present — validating...", req_id)

    if not oauth_provider.validate_bearer(request):
        _LOGGER.info("[%s] [AUTH] OAuth validation FAILED — returning 401", req_id)
        from .oauth import build_unauthorized_response
        return build_unauthorized_response(request, oauth_provider)

    _LOGGER.debug("[%s] [AUTH] Bearer token valid — forwarding upstream", req_id)

    # Build forwarded headers
    forward_headers = {}
    skipped = []
    for key, value in request.headers.items():
        if key.lower() in _HOP_BY_HOP:
            skipped.append(key)
            continue
        forward_headers[key] = value

    # Inject upstream token if configured
    upstream_token = data.get("token", "")
    if upstream_token:
        forward_headers["Authorization"] = f"Bearer {upstream_token}"
        _LOGGER.debug("[%s] [FWD-HEADERS] Injected upstream Authorization token", req_id)

    _LOGGER.debug("[%s] [FWD-HEADERS] Forwarding %d headers to %s (skipped: %s)", req_id, len(forward_headers), masked_target, skipped)

    try:
        async with session.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body if body else None,
        ) as upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")
            _LOGGER.debug("[%s] [RESP] HTTP %d from %s (Content-Type: %s)", req_id, upstream_resp.status, masked_target, content_type)
            _LOGGER.debug("[%s] [RESP-HEADERS] %s", req_id, dict(upstream_resp.headers))

            resp_headers = {"Cache-Control": "no-cache, no-transform", "Content-Encoding": "identity"}
            mcp_session = upstream_resp.headers.get("Mcp-Session-Id")
            if mcp_session:
                resp_headers["Mcp-Session-Id"] = mcp_session

            if "text/event-stream" in content_type:
                resp_headers["Content-Type"] = "text/event-stream"
                resp_headers["X-Accel-Buffering"] = "no"
                response = web.StreamResponse(status=upstream_resp.status, headers=resp_headers)
                await response.prepare(request)
                chunk_count = 0
                async for chunk in upstream_resp.content.iter_any():
                    chunk_count += 1
                    _LOGGER.debug("[%s] [RESP-STREAM-CHUNK #%d] %d bytes: %s", req_id, chunk_count, len(chunk), _truncate(chunk, 512))
                    await response.write(chunk)
                await response.write_eof()
                return response
            else:
                resp_body = await upstream_resp.read()
                allowed_types = ("application/json", "text/event-stream")
                if not any(ct in content_type for ct in allowed_types):
                    _LOGGER.warning("[%s] [RESP] Unexpected Content-Type %r — forcing application/json", req_id, content_type)
                    content_type = "application/json"
                resp_headers["Content-Type"] = content_type
                _LOGGER.debug("[%s] [RESP-BODY] HTTP %d, %d bytes: %s", req_id, upstream_resp.status, len(resp_body), _truncate(resp_body))
                return web.Response(status=upstream_resp.status, body=resp_body, headers=resp_headers)

    except aiohttp.ClientConnectorError as err:
        _LOGGER.error("[%s] [UPSTREAM-ERR] Cannot connect to %s: %s", req_id, masked_target, err)
        return web.Response(status=502, text=f"MCP Proxy: cannot connect to upstream ({err})")
    except aiohttp.ServerTimeoutError as err:
        _LOGGER.error("[%s] [UPSTREAM-ERR] Timeout waiting for %s: %s", req_id, masked_target, err)
        return web.Response(status=504, text="MCP Proxy: upstream timeout")
    except aiohttp.ClientError as err:
        _LOGGER.error("[%s] [UPSTREAM-ERR] aiohttp error from %s: %s", req_id, masked_target, err)
        return web.Response(status=502, text=f"MCP Proxy: upstream error ({type(err).__name__})")
    except Exception as err:
        _LOGGER.exception("[%s] [UPSTREAM-ERR] Unexpected error: %s", req_id, err)
        return web.Response(status=500, text="MCP Proxy: internal error")


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.info("MCP Proxy: unloading config entry")
    data = hass.data.pop(DOMAIN, {})
    for slot_info in data.get("slots", {}).values():
        webhook_id = slot_info.get("webhook_id")
        if webhook_id:
            async_unregister(hass, webhook_id)
            _LOGGER.info("MCP Proxy: Slot %s webhook unregistered", slot_info.get("slot"))
    session = data.get("session")
    if session:
        await session.close()
        _LOGGER.info("MCP Proxy: aiohttp session closed")
    return True
