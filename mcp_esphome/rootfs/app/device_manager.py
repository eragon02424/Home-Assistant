"""Device Manager - handles ESPHome device discovery and heartbeat history.

Architecture:
─────────────
STARTUP
  1. _load_heartbeat_history() — creates stubs from disk (initialized=False)
  2. run_initial_discovery() — fetches /devices from ESPHome dashboard:
       - sets address, mac, psk for every device (stubs get initialized=True)
       - devices with state=online get a keepalive task started
  3. start_mdns_listener() — global ServiceBrowser for _esphomelib._tcp.local.

ONLINE DETECTION
  mDNS add_service fires for every announce (independent of previous state).
  If device.initialized and not device.online:
    → TCP ping port 6053 (200ms timeout, 2 retries)
    → success → _mark_online + start keepalive task

OFFLINE DETECTION
  Per-device keepalive task (only for online devices).
  Every keepalive_interval seconds: TCP ping port 6053.
  All retries fail → _mark_offline, task ends.

DISCOVERY LOOP (60s)
  Only registers new devices. Never re-triggers connects for known devices.

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
    # False for stubs from _load_heartbeat_history.
    # mDNS connects are blocked until _discover_new_devices sets this True.
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
        """Open a TCP connection to address:port and close it immediately.
        Returns True if the connection was accepted, False on any error.
        A successful TCP handshake proves the device is reachable.
        We close immediately so the device sees a clean FIN — minimal load.
        """
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

    # ── Discovery ──────────────────────────────────────────────

    async def run_initial_discovery(self):
        """Fetch /devices once before mDNS starts so all devices have
        initialized=True and noise_psk loaded before any mDNS announce fires.
        """
        try:
            await self._discover_new_devices()
        except Exception as err:
            _LOGGER.error("Initial discovery error: %s", err)

    async def run_discovery_loop(self):
        """Poll /devices every 60s for newly configured devices only.
        Sleeps first — initial discovery already ran at startup.
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

        for entry in configured:
            name = entry.get("name")
            if not name:
                continue

            address = entry.get("address") or f"{name}.local"
            mac = entry.get("mac_address", "")
            configuration_file = entry.get("configuration", "")
            api_encrypted = entry.get("api_encrypted", False)
            # ESPHome reports state as string: "online"/"offline"/None
            dashboard_online = entry.get("state") == "online"

            if name in self.devices:
                device = self.devices[name]
                device.address = address
                if mac and not device.mac_address:
                    device.mac_address = mac
                # Initialize stubs created by _load_heartbeat_history
                if not device.initialized:
                    device.configuration_file = configuration_file
                    psk = self._read_noise_psk(configuration_file)
                    device.noise_psk = psk
                    device.initialized = True
                    if psk:
                        _LOGGER.info("Noise PSK loaded (stub) for %s", name)
                    elif api_encrypted:
                        _LOGGER.warning("No noise PSK for encrypted device %s", name)
                    # If dashboard says online and we have no keepalive running, start one
                    if dashboard_online and not device.online:
                        new_online.append(name)
                continue

            # New device
            psk = self._read_noise_psk(configuration_file)
            device = DeviceState(
                name=name,
                address=address,
                ping_index=self._ping_counter,
                configuration_file=configuration_file,
                noise_psk=psk,
                mac_address=mac or None,
                initialized=True,
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
            _LOGGER.info("Starting keepalive for %d online device(s)", len(new_online))
            for name in new_online:
                asyncio.create_task(self._bring_online(name, source="dashboard"))

    # ── Online / Keepalive ───────────────────────────────────────

    async def _bring_online(self, device_name: str, source: str):
        """Confirm device is reachable via TCP ping, then mark online and
        start the keepalive task. Called from both mDNS and dashboard init.
        Uses connecting flag to prevent simultaneous attempts.
        """
        device = self.devices.get(device_name)
        if not device or not device.initialized:
            return
        if device.online or device.connecting:
            return

        device.connecting = True
        try:
            ok = await self._tcp_ping(device.address)
            if not ok:
                _LOGGER.info("TCP ping failed [%s] %s — staying offline", source, device_name)
                return
            _LOGGER.info("ONLINE (%s): %s", source, device_name)
            self._mark_online(device_name)
            device.keepalive_task = asyncio.create_task(
                self._keepalive_loop(device_name)
            )
        finally:
            device.connecting = False

    async def _keepalive_loop(self, device_name: str):
        """TCP-ping device every keepalive_interval seconds.
        All retries exhausted → mark offline, task ends.
        No aioesphomeapi involved — no internal heartbeat conflicts.
        """
        device = self.devices.get(device_name)
        if not device:
            return

        while True:
            await asyncio.sleep(self.keepalive_interval)

            if not device.online:
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

    # ── mDNS listener ──────────────────────────────────────────

    async def start_mdns_listener(self):
        """One global Zeroconf browser for all ESPHome devices.
        add_service fires on every mDNS announce regardless of prior state.
        We check device.online internally to avoid starting duplicate tasks.
        """
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
                device = mgr.devices.get(device_name)
                if device and device.initialized and not device.online:
                    _LOGGER.info("mDNS announce: %s → TCP ping", device_name)
                    asyncio.run_coroutine_threadsafe(
                        mgr._bring_online(device_name, source="ESPHome/mDNS"),
                        mgr._loop,
                    )

            def remove_service(self, zc, type_, name):
                pass  # offline detection via keepalive only

            def update_service(self, zc, type_, name):
                self.add_service(zc, type_, name)

        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _Listener())
        _LOGGER.info("Global mDNS listener started")

    # ── State transitions ────────────────────────────────────────

    def _mark_online(self, device_name: str):
        device = self.devices[device_name]
        now = time.time()
        if not device.online:
            device.online = True
            device.last_seen = now
            device.heartbeat_events.append((now, "connected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)

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
        if device.keepalive_task and not device.keepalive_task.done():
            device.keepalive_task.cancel()

    # ── Public query API ───────────────────────────────────────

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
