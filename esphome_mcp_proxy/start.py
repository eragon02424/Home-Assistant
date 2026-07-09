#!/usr/bin/env python3
"""MCP Proxy — addon startup script.

This addon installs the mcp_proxy webhook custom integration into HA Core
and proxies remote MCP requests to the configured MCP Server addons.

Features:
  1. Up to 10 configurable MCP server slots
  2. OAuth 2.1 protected webhooks (Nabu Casa / reverse proxy compatible)
  3. Auto-installs and updates the mcp_proxy custom integration
  4. Per-slot OAuth credentials and webhook IDs persisted across restarts
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import urlparse


class IntegrationInstall(NamedTuple):
    first_install: bool
    version_changed: bool


if TYPE_CHECKING:
    from typing import TextIO


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log(level: str, message: str, stream: "TextIO | None" = None) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} [{level}] {message}", file=stream, flush=True)


def log_info(message: str) -> None:
    _log("INFO", message)


def log_error(message: str) -> None:
    _log("ERROR", message, sys.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Supervisor API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _supervisor_get(path: str) -> dict | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            f"http://supervisor{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_data = json.loads(resp.read())
            if not isinstance(response_data, dict):
                log_error(f"Supervisor API GET {path}: unexpected response type")
                return None
            data = response_data.get("data", {})
            return data if isinstance(data, dict) else {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log_error(f"Supervisor API GET {path}: {e}")
        return None


def _supervisor_post(path: str, data: dict) -> bool:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return False
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"http://supervisor{path}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status: int = resp.status
            return 200 <= status < 300
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log_error(f"Supervisor API POST {path} ({type(e).__name__}): {e} — {err_body}")
        return False
    except (urllib.error.URLError, TimeoutError) as e:
        log_error(f"Supervisor API POST {path} ({type(e).__name__}): {e}")
        return False


def _ha_core_api(method: str, path: str, data: dict | None = None) -> dict | list | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        log_error(f"HA Core API {method} {path}: SUPERVISOR_TOKEN not set")
        return None
    url = f"http://supervisor/core/api{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result: dict | list = json.loads(resp.read())
            return result
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        log_error(f"HA Core API {method} {path}: HTTPError {e.code} {e.reason} — {body_text}")
        return None
    except urllib.error.URLError as e:
        log_error(f"HA Core API {method} {path}: URLError — {e.reason}")
        return None
    except TimeoutError as e:
        log_error(f"HA Core API {method} {path}: Timeout — {e}")
        return None
    except json.JSONDecodeError as e:
        log_error(f"HA Core API {method} {path}: JSONDecodeError — {e}")
        return None
    except Exception as e:
        log_error(f"HA Core API {method} {path}: Unexpected {type(e).__name__} — {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Nabu Casa URL detection
# ─────────────────────────────────────────────────────────────────────────────

def get_nabu_casa_url() -> str | None:
    cloud_storage = Path("/config/.storage/cloud")
    try:
        if cloud_storage.exists():
            cloud_data = json.loads(cloud_storage.read_text())
            data = cloud_data.get("data", {})
            if data.get("remote_enabled"):
                domain = data.get("remote_domain")
                if domain:
                    return f"https://{domain}"
            else:
                log_info("Nabu Casa remote UI is not enabled")
    except (OSError, json.JSONDecodeError) as e:
        log_info(f"Nabu Casa cloud config not available: {e}")
    return None


def _resolve_remote_url(remote_url: str) -> str | None:
    if remote_url and remote_url.strip():
        url = remote_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        return url
    return get_nabu_casa_url()


# ─────────────────────────────────────────────────────────────────────────────
# OAuth credential persistence
# ─────────────────────────────────────────────────────────────────────────────

def _regenerate_oauth_creds(data_dir: Path) -> None:
    creds_file = data_dir / "mcp_proxy_oauth_creds.json"
    try:
        if creds_file.exists():
            creds_file.unlink()
            log_info("Wiped existing OAuth credentials per regenerate toggle")
    except OSError as e:
        log_error(f"Failed to wipe OAuth creds ({type(e).__name__}): {e}")


def _clear_regenerate_toggle(current_config: dict) -> bool:
    new_options = dict(current_config)
    new_options["regenerate_oauth_creds"] = False
    return _supervisor_post("/addons/self/options", {"options": new_options})


def _resolve_oauth_creds(
    data_dir: Path, configured_id: str, configured_secret: str
) -> tuple[str, str]:
    creds_file = data_dir / "mcp_proxy_oauth_creds.json"
    stored: dict = {}
    if creds_file.exists():
        try:
            loaded = json.loads(creds_file.read_text())
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not read existing OAuth creds ({type(e).__name__}): {e}")

    final_id = configured_id.strip() or stored.get("client_id", "")
    final_secret = configured_secret.strip() or stored.get("client_secret", "")

    if not final_id:
        final_id = "mcpproxy-" + secrets.token_hex(16)
        log_info("Generated new OAuth Client ID (no value configured or stored)")
    if not final_secret:
        final_secret = secrets.token_urlsafe(32)
        log_info("Generated new OAuth Client Secret")

    needs_write = (
        stored.get("client_id") != final_id
        or stored.get("client_secret") != final_secret
    )
    if needs_write:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            creds_file.write_text(
                json.dumps({"client_id": final_id, "client_secret": final_secret})
            )
            try:
                creds_file.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            log_error(f"Failed to persist OAuth creds ({type(e).__name__}): {e}")
            return "", ""

    return final_id, final_secret


# ─────────────────────────────────────────────────────────────────────────────
# Webhook ID persistence
# ─────────────────────────────────────────────────────────────────────────────

def _get_or_create_webhook_id(data_dir: Path) -> str:
    wh_file = data_dir / "mcp_proxy_webhook_id.txt"
    if wh_file.exists():
        try:
            wid = wh_file.read_text().strip()
            if wid:
                return wid
        except OSError:
            pass
    wid = f"mcp_{secrets.token_hex(16)}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        wh_file.write_text(wid)
    except OSError as e:
        log_error(f"Failed to save webhook ID: {e}")
    return wid


# ─────────────────────────────────────────────────────────────────────────────
# Integration install/update
# ─────────────────────────────────────────────────────────────────────────────

def _install_integration() -> IntegrationInstall:
    """Install/update the mcp_proxy custom component into HA config dir."""
    src = Path("/opt/mcp_proxy")
    dst = Path("/config/custom_components/mcp_proxy")

    if not src.exists():
        log_error("Integration source not found at /opt/mcp_proxy")
        return IntegrationInstall(False, False)

    Path("/config/custom_components").mkdir(parents=True, exist_ok=True)

    first_install = not dst.exists()
    src_manifest = src / "manifest.json"
    dst_manifest = dst / "manifest.json"

    sv: str | None = None
    dv: str | None = None
    if src_manifest.exists():
        try:
            sv = json.loads(src_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse source manifest: {e}")
    if dst_manifest.exists():
        try:
            dv = json.loads(dst_manifest.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not parse destination manifest: {e}")

    versions_differ = sv is not None and dv is not None and sv != dv
    needs_update = first_install or versions_differ or dv is None
    version_changed = versions_differ and not first_install

    if needs_update:
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        log_info(f"Installed mcp_proxy integration (v{sv} -> /config/custom_components/mcp_proxy/)")
    else:
        log_info(f"mcp_proxy integration up to date (version {dv})")

    return IntegrationInstall(first_install, version_changed)


# ─────────────────────────────────────────────────────────────────────────────
# Config entry management
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_config_entry(retries: int = 12, delay: int = 10) -> bool:
    time.sleep(5)
    for attempt in range(1, retries + 1):
        entries = _ha_core_api("GET", "/config/config_entries/entry")
        if entries is not None:
            for entry in entries:
                if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
                    log_info("mcp_proxy config entry exists")
                    return True

            log_info(f"Creating config entry (attempt {attempt}/{retries})...")
            flow = _ha_core_api(
                "POST", "/config/config_entries/flow", {"handler": "mcp_proxy"}
            )
            if flow is None:
                if attempt < retries:
                    time.sleep(delay)
                continue
            if not isinstance(flow, dict):
                continue

            rtype = flow.get("type")
            if rtype in ("abort", "create_entry"):
                log_info("Config entry ready")
                return True
            if rtype == "form" and flow.get("flow_id"):
                complete = _ha_core_api(
                    "POST", f"/config/config_entries/flow/{flow['flow_id']}", {}
                )
                if isinstance(complete, dict) and complete.get("type") == "create_entry":
                    log_info("Config entry created")
                    return True

        if attempt < retries:
            log_info(f"HA not ready, retrying in {delay}s...")
            time.sleep(delay)

    return False


def _remove_config_entry() -> None:
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
            eid = entry.get("entry_id")
            if eid:
                _ha_core_api("DELETE", f"/config/config_entries/entry/{eid}")
                log_info("Removed mcp_proxy config entry")


def _reload_config_entry() -> None:
    entries = _ha_core_api("GET", "/config/config_entries/entry")
    if entries is None:
        return
    for entry in entries:
        if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
            eid = entry.get("entry_id")
            if eid:
                result = _ha_core_api(
                    "POST", f"/config/config_entries/entry/{eid}/reload"
                )
                if result is not None:
                    log_info("Reloaded mcp_proxy config entry")
                else:
                    log_info("Config entry reload returned no response (may be OK)")
                return


# ─────────────────────────────────────────────────────────────────────────────
# Wait for HA restart
# ─────────────────────────────────────────────────────────────────────────────

def _ha_core_api_quiet(method: str, path: str) -> list | dict | None:
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api{path}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result: list | dict = json.loads(resp.read())
            return result
    except Exception:
        return None


def _wait_for_ha_restart(poll_interval: int = 10, timeout: int = 600) -> None:
    log_info("Waiting for Home Assistant to restart...")
    start = time.monotonic()

    while time.monotonic() - start < timeout:
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is None:
            log_info("HA Core is restarting...")
            break
        if isinstance(result, list):
            for entry in result:
                if isinstance(entry, dict) and entry.get("domain") == "mcp_proxy":
                    log_info("Integration already loaded - HA must have restarted")
                    return
        time.sleep(poll_interval)

    while time.monotonic() - start < timeout:
        time.sleep(poll_interval)
        result = _ha_core_api_quiet("GET", "/config/config_entries/entry")
        if result is not None:
            log_info("HA Core is back up")
            return

    log_info("Timed out waiting for HA restart - continuing anyway")


# ─────────────────────────────────────────────────────────────────────────────
# Health check
# ─────────────────────────────────────────────────────────────────────────────

def _health_check(target_url: str) -> bool:
    try:
        parsed = urlparse(target_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8099
        with socket.create_connection((host, port), timeout=5):
            return True
    except (OSError, TimeoutError):
        return False


def _identify_server(target_url: str, token: str = "") -> str | None:
    """Speak a real MCP 'initialize' handshake to the target and return
    'name version' from its serverInfo, or None if it doesn't answer like
    an MCP server (e.g. a plain REST API listening on the same port)."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcp-proxy", "version": "3.3.0"},
        },
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(target_url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    # Some MCP servers (streamable-http) wrap the JSON in an SSE frame
    # ("event: message\ndata: {...}"), so pull out the data line.
    json_line = body
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            json_line = line[len("data:"):].strip()

    try:
        data = json.loads(json_line)
    except json.JSONDecodeError:
        return None

    server_info = (data.get("result") or {}).get("serverInfo") or {}
    name = server_info.get("name")
    version = server_info.get("version")
    if not name:
        return None
    return f"{name} {version}" if version else name


# ─────────────────────────────────────────────────────────────────────────────
# Addon auto-discovery via Supervisor API
# ─────────────────────────────────────────────────────────────────────────────

def _discover_addon_url(slug: str) -> str | None:
    data = _supervisor_get(f"/addons/{slug}/info")
    if not data:
        log_error(f"Could not get addon info for slug '{slug}' from Supervisor API")
        return None

    network = data.get("network") or {}
    port: int | None = None
    for container_port, host_port in network.items():
        if host_port:
            try:
                port = int(str(container_port).split("/")[0])
                break
            except (ValueError, AttributeError):
                continue

    if port is None:
        port = 8099
        log_info(f"No network port found for '{slug}', assuming port {port}")

    hostname = f"addon_{slug}"
    url = f"http://{hostname}:{port}/mcp"
    log_info(f"Discovered addon '{slug}' at {url}")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# OAuth probe
# ─────────────────────────────────────────────────────────────────────────────

def _probe_oauth_active() -> bool:
    result = _ha_core_api("GET", "/api/mcp_proxy/oauth/slot1/protected-resource")
    active = isinstance(result, dict) and "authorization_servers" in result
    if active:
        log_info("OAuth probe: active")
    else:
        log_error(f"OAuth probe: NOT reachable. Response was: {result!r}")
    return active


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _load_servers(config: dict) -> list[dict]:
    servers = []
    for i in range(1, 11):
        enabled = bool(config.get(f"server_{i}_enabled", False))
        url = str(config.get(f"server_{i}_url", "")).strip()
        token = str(config.get(f"server_{i}_token", "")).strip()
        if enabled and url:
            servers.append({"slot": i, "url": url, "token": token})
        elif enabled and not url:
            log_error(f"Server slot {i} is enabled but has no URL - skipping.")
    return servers


def _get_or_create_webhook_id_for_slot(data_dir: Path, slot: int) -> str:
    wh_file = data_dir / f"mcp_proxy_webhook_slot_{slot}.txt"
    if wh_file.exists():
        try:
            wid = wh_file.read_text().strip()
            if wid:
                return wid
        except OSError:
            pass
    wid = f"mcp_{secrets.token_hex(16)}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        wh_file.write_text(wid)
    except OSError as e:
        log_error(f"Failed to save webhook ID for slot {slot}: {e}")
    return wid


def _resolve_oauth_creds_for_slot(data_dir: Path, slot: int) -> tuple[str, str]:
    creds_file = data_dir / f"mcp_proxy_oauth_creds_slot_{slot}.json"
    stored: dict = {}
    if creds_file.exists():
        try:
            loaded = json.loads(creds_file.read_text())
            if isinstance(loaded, dict):
                stored = loaded
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Could not read OAuth creds for slot {slot}: {e}")

    client_id = stored.get("client_id", "")
    client_secret = stored.get("client_secret", "")

    if not client_id:
        client_id = f"mcpproxy{slot}-" + secrets.token_hex(16)
        log_info(f"Slot {slot}: Generated new OAuth Client ID")
    if not client_secret:
        client_secret = secrets.token_urlsafe(32)
        log_info(f"Slot {slot}: Generated new OAuth Client Secret")

    if stored.get("client_id") != client_id or stored.get("client_secret") != client_secret:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            creds_file.write_text(
                json.dumps({"client_id": client_id, "client_secret": client_secret})
            )
            try:
                creds_file.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            log_error(f"Failed to persist OAuth creds for slot {slot}: {e}")
            return "", ""

    return client_id, client_secret


def main() -> int:
    log_info("Starting MCP Proxy addon...")

    config_file = Path("/data/options.json")
    data_dir = Path("/data")
    remote_url = ""
    config: dict = {}

    if config_file.exists():
        try:
            config = json.load(config_file.open())
            remote_url = config.get("remote_url", "")
        except (OSError, json.JSONDecodeError) as e:
            log_error(f"Failed to read config ({type(e).__name__}): {e}")

    servers = _load_servers(config)
    if not servers:
        log_error(
            "No active MCP Server slots configured. "
            "Enable at least one server slot and set its URL in the addon config."
        )
        return 1

    log_info(f"Active server slots: {[s['slot'] for s in servers]}")
    resolved_remote = _resolve_remote_url(remote_url)

    proxy_servers = []
    for server in servers:
        slot = server["slot"]
        url = server["url"]
        token = server["token"]

        log_info(f"Slot {slot}: URL = {url}")

        if not _health_check(url):
            log_error(f"Slot {slot}: Cannot reach MCP Server at {url}. Continuing anyway.")
        else:
            identity = _identify_server(url, token)
            if identity:
                log_info(f"Slot {slot}: MCP Server is reachable ({identity})")
            else:
                log_info(f"Slot {slot}: MCP Server is reachable (port open, but no valid MCP handshake - is this really an MCP endpoint?)")

        webhook_id = _get_or_create_webhook_id_for_slot(data_dir, slot)
        client_id, client_secret = _resolve_oauth_creds_for_slot(data_dir, slot)
        if not client_id or not client_secret:
            log_error(f"Slot {slot}: Failed to resolve OAuth credentials - skipping")
            continue

        if token:
            log_info(f"Slot {slot}: upstream token configured")

        proxy_servers.append({
            "slot": slot,
            "url": url,
            "token": token,
            "webhook_id": webhook_id,
            "client_id": client_id,
            "client_secret": client_secret,
        })

    if not proxy_servers:
        log_error("No server slots could be configured. Check addon log for errors.")
        return 1

    proxy_config: dict = {
        "servers": [
            {
                "slot": s["slot"],
                "target_url": s["url"],
                "webhook_id": s["webhook_id"],
                "token": s["token"],
                "oauth": {
                    "client_id": s["client_id"],
                    "client_secret": s["client_secret"],
                },
            }
            for s in proxy_servers
        ]
    }
    if resolved_remote:
        proxy_config["public_base_url"] = resolved_remote

    proxy_config_file = Path("/config/.mcp_proxy_config.json")
    try:
        proxy_config_file.write_text(json.dumps(proxy_config))
        log_info(f"Wrote proxy config to {proxy_config_file} ({len(proxy_servers)} server(s))")
    except OSError as e:
        log_error(f"Failed to write proxy config: {e}")
        return 1

    first_install, version_changed = _install_integration()

    if version_changed:
        log_info("Integration updated - HA restart required to load new code")
        _ha_core_api(
            "POST", "/services/persistent_notification/create",
            {
                "title": "MCP Proxy: Restart Required",
                "message": "The MCP Proxy integration was updated. Please restart Home Assistant.",
                "notification_id": "mcp_proxy_update",
            },
        )

    if first_install:
        log_info("First install - HA restart required to load the integration")
        _ha_core_api(
            "POST", "/services/persistent_notification/create",
            {
                "title": "MCP Proxy: Restart Required",
                "message": "The MCP Proxy integration was installed. Please restart Home Assistant (Settings > System > Restart).",
                "notification_id": "mcp_proxy_restart",
            },
        )
        _wait_for_ha_restart()
        if not _ensure_config_entry():
            log_error("Could not create config entry after HA restart. Webhook is NOT active.")
        else:
            _reload_config_entry()
            _ha_core_api("POST", "/services/persistent_notification/dismiss", {"notification_id": "mcp_proxy_restart"})
            log_info("Setup completed after HA restart")
    else:
        if not _ensure_config_entry():
            log_error("Could not create config entry. Webhook is NOT active.")
        else:
            _reload_config_entry()
            _ha_core_api("POST", "/services/persistent_notification/dismiss", {"notification_id": "mcp_proxy_restart"})

    oauth_restart_marker = Path("/config/.mcp_proxy_oauth_restart_required")
    log_info("Waiting 10s for HA to finish loading OAuth views...")
    time.sleep(10)

    if _probe_oauth_active():
        try:
            oauth_restart_marker.unlink(missing_ok=True)
        except OSError:
            pass
    else:
        log_error(
            "OAuth probe failed - check HA logs for mcp_proxy errors. "
            "If OAuth is not working, restart Home Assistant."
        )

    log_info("")
    log_info("=" * 70)
    for s in proxy_servers:
        webhook_path = f"/api/webhook/{s['webhook_id']}"
        remote_url_full = f"{resolved_remote}{webhook_path}" if resolved_remote else f"https://<your-external-url>{webhook_path}"
        identity = _identify_server(s["url"], s["token"])
        identity_suffix = f"  [{identity}]" if identity else "  [no MCP handshake - check target]"
        log_info(f"  Slot {s['slot']}: {s['url']}{identity_suffix}")
        log_info(f"    Remote URL:          {remote_url_full}")
        log_info(f"    OAuth Client ID:     {s['client_id']}")
        log_info(f"    OAuth Client Secret: {s['client_secret']}")
        log_info("")
    log_info("  Copy each Remote URL + OAuth credentials into Claude.ai")
    log_info("  (Claude.ai: connector > Advanced settings)")
    log_info("=" * 70)
    log_info("")

    log_info("Entering keep-alive loop (health check every 60s)...")
    consecutive_failures: dict[int, int] = {s["slot"]: 0 for s in proxy_servers}
    while True:
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            log_info("Shutting down...")
            break

        for s in proxy_servers:
            slot = s["slot"]
            if _health_check(s["url"]):
                if consecutive_failures[slot] > 0:
                    identity = _identify_server(s["url"], s["token"])
                    suffix = f" ({identity})" if identity else " (port open, but no valid MCP handshake)"
                    log_info(f"Slot {slot}: MCP Server reachable again (was down for {consecutive_failures[slot]} checks){suffix}")
                consecutive_failures[slot] = 0
            else:
                consecutive_failures[slot] += 1
                n = consecutive_failures[slot]
                if n == 1:
                    log_error(f"Slot {slot}: MCP Server unreachable at {s['url']}")
                elif n % 5 == 0:
                    log_error(f"Slot {slot}: MCP Server still unreachable after {n} checks")

    _remove_config_entry()
    log_info("MCP Proxy stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
