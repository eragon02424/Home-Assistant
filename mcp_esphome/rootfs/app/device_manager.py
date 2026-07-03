"""Device Manager - handles ESPHome device discovery and heartbeat history.

Architecture:
─────────────
STARTUP
  1. _load_heartbeat_history() — creates stubs from disk (initialized=False)
  2. run_initial_discovery() — fetches /devices from ESPHome dashboard:
       - sets address, mac, psk for every device (stubs get initialized=True)
       - devices with state=online: TCP ping to confirm reachability,
         then _mark_online + keepalive task
  3. start_mdns_listener() — global ServiceBrowser for _esphomelib._tcp.local.

ONLINE DETECTION (after startup)
  mDNS add_service fires for every announce (independent of previous state).
  mDNS announce = device is on WiFi = sufficient proof of online.
  No TCP ping needed — _mark_online immediately + start keepalive task.

OFFLINE DETECTION
  Per-device keepalive task (only for online devices).
  Every keepalive_interval seconds: TCP ping port 6053.
  All retries fail → _mark_offline, task ends.

DISCOVERY LOOP (60s)
  Only registers new devices. Never re-triggers connects for known devices.
  Also checks if ESPHome reports state=online for devices we think are offline.

ZERO aioesphomeapi — no internal heartbeat conflicts.
ZERO CPU for offline devices.
"""
import asyncio
import json
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

try:
    from zeroconf import ServiceBrowser
    from zeroconf.asyncio import AsyncZeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

_LOGGER = logging.getLogger("mcp_esphome.device_manager")

STORAGE_DIR = Path("/data/mcp_esphome")
ESPHOME_CONFIG_DIR = Path("/config/esphome")
DISCOVERY_INTERVAL_SECONDS = 60
BEARER_TOKEN_FILE = STORAGE_DIR / "bearer_token.txt"
MDNS_SERVICE_TYPE = "_esphomelib._tcp.local."

_NOISE_KEY_RE = re.compile(
    r"encryption:\s*\n\s*key:\s*[\"']?([A-Za-z0-9+/=]+)[\"']?"
)


def load_or_generate_bearer_token(configured_token: str) -> str:
    if configured_token:
        return configured_token
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    if BEARER_TOKEN_FILE.exists():
        token = BEARER_TOKEN_FILE.read_text().strip()
        if token:
            return token
    alphabet = string.ascii_letters + string.digits
    token = "mcp_" + "".join(secrets.choice(alphabet) for _ in range(40))
    BEARER_TOKEN_FILE.write_text(token)
    _LOGGER.warning("=" * 60)
    _LOGGER.warning("Auto-generated Bearer Token: %s", token)
    _LOGGER.warning("Add this token to your Claude MCP connector.")
    _LOGGER.warning("=" * 60)
    return token


@dataclass
class DeviceState:
    name: str
    address: str
    ping_index: int = 0
    configuration_file: str = ""
    noise_psk: Optional[str] = None
    mac_address: Optional[str] = None
    online: bool = False
    last_seen: Optional[float] = None
    last_disconnect: Optional[float] = None
    initialized: bool = False
    heartbeat_events: list = field(default_factory=list)
    keepalive_task: Optional[asyncio.Task] = None
    connecting: bool = False


class DeviceManager:
    def __init__(
        self,
        esphome_dashboard_url: str,
        log_retention_hours: int,
        heartbeat_retention_days: int,
        keepalive_interval: int = 10,
        keepalive_retries: int = 2,
        keepalive_ping_timeout_ms: int = 200,
        bearer_token: str = "",
    ):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
        self.keepalive_interval = keepalive_interval
        self.keepalive_retries = keepalive_retries
        self.keepalive_ping_timeout = keepalive_ping_timeout_ms / 1000.0
        self.bearer_token = load_or_generate_bearer_token(bearer_token)
        self.devices: dict[str, DeviceState] = {}
        self._ping_counter = 0
        self._azeroconf: Optional[object] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_heartbeat_history()

    # ── Persistence ──────────────────────────────────────────────

    def _heartbeat_file(self, device_name: str) -> Path:
        return STORAGE_DIR / f"heartbeat_{device_name.replace('/', '_')}.json"

    def _load_heartbeat_history(self):
        for f in STORAGE_DIR.glob("heartbeat_*.json"):
            try:
                device_name = f.stem.replace("heartbeat_", "")
                with open(f) as fh:
                    events = json.load(fh)
                if device_name not in self.devices:
                    self.devices[device_name] = DeviceState(
                        name=device_name, address="", initialized=False
                    )
                self.devices[device_name].heartbeat_events = events
            except Exception as err:
                _LOGGER.warning("Failed to load heartbeat history from %s: %s", f, err)

    def _save_heartbeat_history(self, device_name: str):
        try:
            device = self.devices[device_name]
            with open(self._heartbeat_file(device_name), "w") as f:
                json.dump(device.heartbeat_events, f)
        except Exception as err:
            _LOGGER.error("Failed to save heartbeat history for %s: %s", device_name, err)

    def _prune_heartbeat(self, device_name: str):
        device = self.devices[device_name]
        cutoff = time.time() - self.heartbeat_retention_seconds
        device.heartbeat_events = [e for e in device.heartbeat_events if e[0] >= cutoff]

    # ── Noise PSK ─────────────────────────────────────────────

    def _read_noise_psk(self, configuration_file: str) -> Optional[str]:
        if not configuration_file:
            return None
        filename = Path(configuration_file).name
        path = ESPHOME_CONFIG_DIR / filename
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as err:
            _LOGGER.info("Could not read %s: %s", path, err)
            return None
        match = _NOISE_KEY_RE.search(content)
        return match.group(1) if match else None

    # ── TCP ping ──────────────────────────────────────────────

    async def _tcp_ping(self, address: str, port: int = 6053) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port),
                timeout=self.keepalive_ping_timeout,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    # ── Discovery ─────────────────────────────────────────────

    async def run_initial_discovery(self):
        try:
            await self._discover_new_devices()
        except Exception as err:
            _LOGGER.error("Initial discovery error: %s", err)

    async def run_discovery_loop(self):
        """Every 60s: register new devices AND recover offline devices
        that ESPHome reports as online (fallback for missed mDNS announces).
        """
        while True:
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)
            try:
                await self._discover_new_devices()
            except Exception as err:
                _LOGGER.error("Discovery loop error: %s", err)

    async def _discover_new_devices(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.esphome_dashboard_url}/devices", timeout=10
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("Cannot reach ESPHome dashboard: %s", err)
            return

        configured = data.get("configured", []) if isinstance(data, dict) else []
        new_online: list[str] = []
        recover_online: list[str] = []

        for entry in configured:
            name = entry.get("name")
            if not name:
                continue

            address = entry.get("address") or f"{name}.local"
            mac = entry.get("mac_address", "")
            configuration_file = entry.get("configuration", "")
            api_encrypted = entry.get("api_encrypted", False)
            dashboard_online = entry.get("state") == "online"

            if name in self.devices:
                device = self.devices[name]
                device.address = address
                if mac and not device.mac_address:
                    device.mac_address = mac
                if not device.initialized:
                    device.configuration_file = configuration_file
                    psk = self._read_noise_psk(configuration_file)
                    device.noise_psk = psk
                    device.initialized = True
                    if psk:
                        _LOGGER.info("Noise PSK loaded (stub) for %s", name)
                    elif api_encrypted:
                        _LOGGER.warning("No noise PSK for encrypted device %s", name)
                    if dashboard_online and not device.online:
                        new_online.append(name)
                else:
                    # Fallback: ESPHome says online but we think offline
                    if dashboard_online and not device.online and not device.connecting:
                        task_done = (device.keepalive_task is None
                                     or device.keepalive_task.done())
                        _LOGGER.info(
                            "Dashboard recovery check: %s online=%s connecting=%s "
                            "task_done=%s",
                            name, device.online, device.connecting, task_done
                        )
                        if task_done:
                            recover_online.append(name)
                continue

            psk = self._read_noise_psk(configuration_file)
            device = DeviceState(
                name=name, address=address, ping_index=self._ping_counter,
                configuration_file=configuration_file, noise_psk=psk,
                mac_address=mac or None, initialized=True,
            )
            self._ping_counter += 1

            if psk:
                _LOGGER.info("Noise PSK loaded for %s", name)
            elif api_encrypted:
                _LOGGER.warning("No noise PSK for encrypted device %s (config: %s)",
                                name, configuration_file)

            self.devices[name] = device

            if dashboard_online:
                new_online.append(name)
                _LOGGER.info("New device (online): %s @ %s", name, address)
            else:
                _LOGGER.debug("New device (offline): %s — waiting for mDNS", name)

        if new_online:
            _LOGGER.info("Dashboard init: TCP ping for %d online device(s)", len(new_online))
            for name in new_online:
                asyncio.create_task(self._bring_online_with_ping(name))

        if recover_online:
            _LOGGER.info("Dashboard recovery: %d device(s) missed mDNS", len(recover_online))
            for name in recover_online:
                asyncio.create_task(self._bring_online_with_ping(name))

    # ── Online / Keepalive ────────────────────────────────────

    async def _bring_online_with_ping(self, device_name: str):
        """TCP ping confirms reachability before marking online.
        Used at startup and as dashboard recovery fallback.
        """
        device = self.devices.get(device_name)
        if not device or device.online or device.connecting:
            return
        device.connecting = True
        try:
            ok = await self._tcp_ping(device.address)
            if not ok:
                _LOGGER.info("TCP ping failed (dashboard) %s — staying offline", device_name)
                return
            _LOGGER.info("ONLINE (dashboard): %s", device_name)
            self._mark_online(device_name)
            device.keepalive_task = asyncio.create_task(self._keepalive_loop(device_name))
            _LOGGER.info("Keepalive task started for %s", device_name)
        finally:
            device.connecting = False

    def _bring_online_from_mdns(self, device_name: str):
        """Called via loop.call_soon_threadsafe from Zeroconf thread.
        mDNS announce = device is on WiFi = sufficient proof of online.
        No TCP ping — mark online immediately, keepalive verifies via TCP.
        """
        device = self.devices.get(device_name)
        if not device:
            _LOGGER.info("mDNS [%s]: device not registered yet", device_name)
            return
        if not device.initialized:
            _LOGGER.info("mDNS [%s]: not yet initialized, ignoring", device_name)
            return
        if device.online:
            _LOGGER.debug("mDNS [%s]: already online, ignoring", device_name)
            return
        if device.connecting:
            _LOGGER.info("mDNS [%s]: connect already in progress, ignoring", device_name)
            return

        task_done = device.keepalive_task is None or device.keepalive_task.done()
        _LOGGER.info(
            "mDNS [%s]: marking online (connecting=%s task_done=%s)",
            device_name, device.connecting, task_done
        )

        device.connecting = True
        try:
            self._mark_online(device_name)
            device.keepalive_task = asyncio.create_task(self._keepalive_loop(device_name))
            _LOGGER.info("Keepalive task started for %s (mDNS)", device_name)
        finally:
            device.connecting = False

    async def _keepalive_loop(self, device_name: str):
        device = self.devices.get(device_name)
        if not device:
            return

        _LOGGER.info("Keepalive loop running for %s", device_name)
        while True:
            await asyncio.sleep(self.keepalive_interval)

            if not device.online:
                _LOGGER.info("Keepalive loop: %s is offline, exiting", device_name)
                break

            success = False
            for attempt in range(1, self.keepalive_retries + 1):
                ok = await self._tcp_ping(device.address)
                if ok:
                    device.last_seen = time.time()
                    _LOGGER.info("Keepalive OK: %s (attempt %d/%d)",
                                 device_name, attempt, self.keepalive_retries)
                    success = True
                    break
                _LOGGER.info("Keepalive attempt %d/%d failed: %s",
                             attempt, self.keepalive_retries, device_name)
                if attempt < self.keepalive_retries:
                    await asyncio.sleep(self.keepalive_ping_timeout)

            if not success:
                _LOGGER.info("OFFLINE (Keepalive): %s", device_name)
                self._mark_offline(device_name)
                break

        _LOGGER.info("Keepalive loop ended for %s (task_done=%s)",
                     device_name,
                     device.keepalive_task.done() if device.keepalive_task else True)

    # ── mDNS listener ─────────────────────────────────────────

    async def start_mdns_listener(self):
        if not HAS_ZEROCONF:
            _LOGGER.warning("zeroconf not available — mDNS wakeup disabled")
            return

        self._loop = asyncio.get_event_loop()
        self._azeroconf = AsyncZeroconf()
        zc = self._azeroconf.zeroconf  # type: ignore[attr-defined]
        mgr = self

        class _Listener:
            def add_service(self, zc, type_, name):
                device_name = (name
                               .replace(f".{MDNS_SERVICE_TYPE}", "")
                               .replace(f".{type_}", "")
                               .rstrip("."))
                _LOGGER.info("mDNS add_service fired: %s", device_name)
                mgr._loop.call_soon_threadsafe(
                    mgr._bring_online_from_mdns, device_name
                )

            def remove_service(self, zc, type_, name):
                device_name = (name
                               .replace(f".{MDNS_SERVICE_TYPE}", "")
                               .replace(f".{type_}", "")
                               .rstrip("."))
                _LOGGER.debug("mDNS remove_service fired: %s", device_name)

            def update_service(self, zc, type_, name):
                device_name = (name
                               .replace(f".{MDNS_SERVICE_TYPE}", "")
                               .replace(f".{type_}", "")
                               .rstrip("."))
                _LOGGER.info("mDNS update_service fired: %s", device_name)
                mgr._loop.call_soon_threadsafe(
                    mgr._bring_online_from_mdns, device_name
                )

        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _Listener())
        _LOGGER.info("Global mDNS listener started")

    # ── State transitions ─────────────────────────────────────

    def _mark_online(self, device_name: str):
        device = self.devices[device_name]
        now = time.time()
        if not device.online:
            device.online = True
            device.last_seen = now
            device.heartbeat_events.append((now, "connected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)
            _LOGGER.info("State → ONLINE: %s", device_name)

    def _mark_offline(self, device_name: str):
        device = self.devices.get(device_name)
        if not device:
            return
        now = time.time()
        if device.online:
            device.online = False
            device.last_disconnect = now
            device.heartbeat_events.append((now, "disconnected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)
            _LOGGER.info("State → OFFLINE: %s", device_name)
        if device.keepalive_task and not device.keepalive_task.done():
            device.keepalive_task.cancel()
            _LOGGER.info("Keepalive task cancelled for %s", device_name)
        device.keepalive_task = None

    # ── Public query API ──────────────────────────────────────

    def get_bearer_token(self) -> str:
        return self.bearer_token

    def list_devices(self) -> list[dict]:
        return [
            {
                "name": name,
                "address": device.address,
                "online": device.online,
                "last_seen": device.last_seen,
                "has_noise_key": device.noise_psk is not None,
                "mac_address": device.mac_address,
            }
            for name, device in self.devices.items()
            if device.initialized
        ]

    def get_last_seen(self, device_name: str) -> Optional[dict]:
        device = self.devices.get(device_name)
        if not device:
            return None
        return {
            "online": device.online,
            "last_seen": device.last_seen,
            "last_disconnect": device.last_disconnect,
        }

    def get_uptime_pattern(self, device_name: str, last_n_cycles: int = 10) -> list[dict]:
        device = self.devices.get(device_name)
        if not device:
            return []
        events = device.heartbeat_events[-(last_n_cycles * 2):]
        cycles, current = [], {}
        for ts, kind in events:
            if kind == "connected":
                current = {"connected_at": ts}
            elif kind == "disconnected" and "connected_at" in current:
                current["disconnected_at"] = ts
                current["duration_seconds"] = ts - current["connected_at"]
                cycles.append(current)
                current = {}
        return cycles[-last_n_cycles:]
