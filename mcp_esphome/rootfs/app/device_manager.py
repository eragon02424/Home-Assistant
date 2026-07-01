"""Device Manager - handles ESPHome device discovery, connections, logs and heartbeat history.

Architecture:
─────────────
STARTUP
  1. Query ESPHome dashboard /devices — register all configured devices.
  2. For each device fire one independent connect task.
     Each task is completely isolated: a failure in one never affects another.

ONLINE DETECTION
  Two independent sources can bring a device online:
  a) Initial connect task at startup (source="dashboard")
  b) Global mDNS listener — fires when a device announces _esphomelib._tcp.local.
     (source="ESPHome/mDNS")
  Both call _connect_device() which is guarded per-device so two simultaneous
  triggers for the same device are safe (second one bails immediately).

  Log: "ONLINE (dashboard): <name>"  or  "ONLINE (ESPHome/mDNS): <name>"

OFFLINE DETECTION
  Only the per-device keepalive task detects offline.
  It sends keepalive_retries pings spaced 1 s apart.
  If all retries fail → device marked offline, task ends.
  Log: "OFFLINE (Keepalive): <name>"

DISCOVERY LOOP (60 s)
  Only registers *new* devices that appeared in ESPHome since last check.
  Never re-triggers connects for already-known devices.

ZERO CPU for offline devices: no task, no connection, no polling.
Every device's keepalive runs in its own asyncio.Task — no shared state,
no locks that could block other devices.
"""
import asyncio
import json
import logging
import re
import secrets
import string
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

try:
    from aioesphomeapi import APIClient
    HAS_AIOESPHOMEAPI = True
except ImportError:
    APIClient = None
    HAS_AIOESPHOMEAPI = False

try:
    from zeroconf import ServiceBrowser
    from zeroconf.asyncio import AsyncZeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

try:
    import aiomqtt
except ImportError:
    aiomqtt = None

_LOGGER = logging.getLogger("mcp_esphome.device_manager")

STORAGE_DIR = Path("/data/mcp_esphome")
ESPHOME_CONFIG_DIR = Path("/config/esphome")
DISCOVERY_INTERVAL_SECONDS = 60
BEARER_TOKEN_FILE = STORAGE_DIR / "bearer_token.txt"
MDNS_SERVICE_TYPE = "_esphomelib._tcp.local."
CONNECT_TIMEOUT = 8.0  # TCP connect timeout (seconds)

_NOISE_KEY_RE = re.compile(
    r"api:\s*\n(?:.*\n)*?\s*encryption:\s*\n\s*key:\s*[\"']?([A-Za-z0-9+/=]+)[\"']?",
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
    model: Optional[str] = None
    online: bool = False
    last_seen: Optional[float] = None
    last_disconnect: Optional[float] = None
    mqtt_discovery_published: bool = False
    first_ping_done: bool = False
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=5000))
    heartbeat_events: list = field(default_factory=list)
    client: Optional[object] = None
    # One keepalive task per device — started on connect, stopped on disconnect.
    # Never shared with any other device.
    keepalive_task: Optional[asyncio.Task] = None
    # Prevents two simultaneous connect attempts for the same device
    # (e.g. mDNS fires while startup connect is still in progress).
    # Uses a simple flag instead of asyncio.Lock to avoid any waiting.
    connecting: bool = False


class DeviceManager:
    def __init__(
        self,
        esphome_dashboard_url: str,
        log_retention_hours: int,
        heartbeat_retention_days: int,
        keepalive_interval: int = 10,
        keepalive_retries: int = 5,
        bearer_token: str = "",
        mqtt_host: str = "core-mosquitto",
        mqtt_port: int = 1883,
    ):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.log_retention_seconds = log_retention_hours * 3600
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
        self.keepalive_interval = keepalive_interval
        self.keepalive_retries = keepalive_retries
        # Each ping attempt waits 1 s for a response
        self.keepalive_ping_timeout = 1
        self.bearer_token = load_or_generate_bearer_token(bearer_token)
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
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
                    self.devices[device_name] = DeviceState(name=device_name, address="")
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

    # ── Noise PSK ───────────────────────────────────────────────

    def _read_noise_psk(self, configuration_file: str) -> Optional[str]:
        if not configuration_file:
            return None
        path = ESPHOME_CONFIG_DIR / configuration_file
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as err:
            _LOGGER.debug("Could not read %s for noise key: %s", path, err)
            return None
        match = _NOISE_KEY_RE.search(content)
        return match.group(1) if match else None

    # ── MQTT Discovery ────────────────────────────────────────────

    async def _publish_mqtt_discovery(self, device: DeviceState):
        if aiomqtt is None:
            return
        identifier = device.mac_address.replace(":", "") if device.mac_address else f"esphome_{device.name}"
        unique_id = f"mcp_esphome_{identifier}_online"
        state_topic = f"mcp_esphome/{device.name}/online"
        config_topic = f"homeassistant/binary_sensor/{unique_id}/config"
        device_block: dict = {"name": device.name, "identifiers": [identifier]}
        if device.mac_address:
            device_block["connections"] = [["mac", device.mac_address]]
        if device.model:
            device_block["model"] = device.model
        payload = {
            "name": "Online", "unique_id": unique_id, "device_class": "connectivity",
            "state_topic": state_topic, "payload_on": "ON", "payload_off": "OFF",
            "device": device_block,
        }
        try:
            async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                await client.publish(config_topic, payload=json.dumps(payload), retain=True)
                await client.publish(state_topic, payload="ON" if device.online else "OFF", retain=True)
            _LOGGER.info("MQTT Discovery published for %s", device.name)
            device.mqtt_discovery_published = True
        except Exception as err:
            _LOGGER.error("MQTT discovery failed for %s: %s", device.name, err)

    async def _publish_mqtt_state(self, device: DeviceState, online: bool):
        if aiomqtt is None or not device.mqtt_discovery_published:
            return
        try:
            async with aiomqtt.Client(hostname=self.mqtt_host, port=self.mqtt_port) as client:
                await client.publish(
                    f"mcp_esphome/{device.name}/online",
                    payload="ON" if online else "OFF",
                    retain=True,
                )
        except Exception as err:
            _LOGGER.warning("MQTT state publish failed for %s: %s", device.name, err)

    # ── Dashboard discovery ──────────────────────────────────────

    async def run_discovery_loop(self):
        """Poll ESPHome dashboard every 60 s.
        Only registers NEW devices — never re-triggers connects for known ones.
        """
        while True:
            try:
                await self._discover_new_devices()
            except Exception as err:
                _LOGGER.error("Discovery loop error: %s", err)
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)

    async def _discover_new_devices(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.esphome_dashboard_url}/devices", timeout=10) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Dashboard returned HTTP %s", resp.status)
                        return
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("Cannot reach ESPHome dashboard: %s", err)
            return

        for entry in data.get("configured", []) if isinstance(data, dict) else []:
            name = entry.get("name")
            if not name:
                continue
            address = entry.get("address") or f"{name}.local"
            configuration_file = entry.get("configuration", "")
            mac_from_dashboard = entry.get("mac_address", "")
            api_encrypted = entry.get("api_encrypted", False)

            if name not in self.devices:
                # New device — register and attempt first connect
                _LOGGER.info("New device discovered: %s (%s)", name, address)
                device = DeviceState(name=name, address=address, ping_index=self._ping_counter)
                self._ping_counter += 1
                self.devices[name] = device
                if mac_from_dashboard:
                    device.mac_address = mac_from_dashboard
                if api_encrypted:
                    device.noise_psk = self._read_noise_psk(configuration_file)
                device.configuration_file = configuration_file
                # Fire connect task for the new device only
                asyncio.create_task(self._connect_device(name, source="dashboard"))
            else:
                # Known device — just update metadata, never re-trigger connect
                device = self.devices[name]
                device.address = address
                device.configuration_file = configuration_file
                if mac_from_dashboard and not device.mac_address:
                    device.mac_address = mac_from_dashboard
                if api_encrypted and device.noise_psk is None:
                    device.noise_psk = self._read_noise_psk(configuration_file)

    # ── Connect a device ─────────────────────────────────────────

    async def _connect_device(self, device_name: str, source: str):
        """Attempt one TCP connect for a single device.

        Uses a simple boolean flag (device.connecting) instead of asyncio.Lock
        so a second caller returns immediately without waiting — no blocking.
        """
        if not HAS_AIOESPHOMEAPI:
            return

        device = self.devices.get(device_name)
        if not device:
            return

        # Guard: bail immediately if already online or already connecting
        if device.online or device.connecting:
            return
        device.connecting = True

        try:
            client = APIClient(device.address, 6053, None, noise_psk=device.noise_psk)
            try:
                await asyncio.wait_for(client.connect(login=False), timeout=CONNECT_TIMEOUT)
            except Exception as err:
                _LOGGER.debug("Connect failed [%s] %s: %s", source, device_name, err)
                # Device stays offline — mDNS will re-trigger when it comes up
                return

            device.client = client
            _LOGGER.info("ONLINE (%s): %s", source, device_name)
            self._mark_online(device_name)
            client.subscribe_logs(lambda msg, dn=device_name: self._on_log_message(dn, msg))

            # Collect MAC / model on first connect for MQTT discovery
            if not device.first_ping_done:
                try:
                    info = await asyncio.wait_for(
                        client.device_info(), timeout=self.keepalive_ping_timeout
                    )
                    device.first_ping_done = True
                    if not device.mac_address and hasattr(info, "mac_address"):
                        device.mac_address = info.mac_address
                    if not device.model and hasattr(info, "model"):
                        device.model = info.model
                    if not device.mqtt_discovery_published:
                        asyncio.create_task(self._publish_mqtt_discovery(device))
                except Exception:
                    pass  # will retry on next keepalive

            # Start this device's own independent keepalive task
            num_devices = max(len(self.devices), 1)
            stagger = (device.ping_index % num_devices) * (self.keepalive_interval / num_devices)
            device.keepalive_task = asyncio.create_task(
                self._keepalive_loop(device_name, client, stagger if source == "dashboard" else 0)
            )
        finally:
            device.connecting = False

    # ── Keepalive loop (one per online device) ───────────────────

    async def _keepalive_loop(self, device_name: str, client: object, initial_stagger: float):
        """Completely independent task — one per online device, no shared state.

        Ping cadence: wait keepalive_interval, then try up to keepalive_retries
        times with 1 s between each attempt.  If all retries fail → offline.

        Log: "OFFLINE (Keepalive): <name>"
        """
        device = self.devices.get(device_name)
        if not device:
            return

        if initial_stagger > 0:
            await asyncio.sleep(initial_stagger)

        while True:
            # Wait for the configured interval before pinging
            await asyncio.sleep(self.keepalive_interval)

            if not device.online:
                break

            # Try up to keepalive_retries times, 1 s per attempt
            success = False
            for attempt in range(1, self.keepalive_retries + 1):
                try:
                    await asyncio.wait_for(
                        client.device_info(),  # type: ignore[arg-type]
                        timeout=self.keepalive_ping_timeout,
                    )
                    device.last_seen = time.time()
                    _LOGGER.debug("Keepalive OK: %s (attempt %d)", device_name, attempt)
                    success = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.debug(
                        "Keepalive attempt %d/%d failed: %s",
                        attempt, self.keepalive_retries, device_name,
                    )
                    if attempt < self.keepalive_retries:
                        await asyncio.sleep(1)

            if not success:
                _LOGGER.info("OFFLINE (Keepalive): %s", device_name)
                self._mark_offline(device_name)
                device.client = None
                break

    # ── Global mDNS listener ──────────────────────────────────────

    async def start_mdns_listener(self):
        """One global Zeroconf browser for all devices.
        Replaces per-device ReconnectLogic — zero CPU for offline devices.
        """
        if not HAS_ZEROCONF:
            _LOGGER.warning("zeroconf not available — mDNS wakeup disabled")
            return

        self._loop = asyncio.get_event_loop()
        self._azeroconf = AsyncZeroconf()
        zc = self._azeroconf.zeroconf  # type: ignore[attr-defined]
        mgr = self

        class _ESPHomeListener:
            def add_service(self, zc, type_, name):
                # Strip service type suffix to get device name
                device_name = name.replace(f".{MDNS_SERVICE_TYPE}", "").replace(f".{type_}", "").rstrip(".")
                if device_name in mgr.devices and not mgr.devices[device_name].online:
                    _LOGGER.info("mDNS announce received: %s", device_name)
                    asyncio.run_coroutine_threadsafe(
                        mgr._connect_device(device_name, source="ESPHome/mDNS"),
                        mgr._loop,
                    )

            def remove_service(self, zc, type_, name):
                pass  # offline detection via keepalive only

            def update_service(self, zc, type_, name):
                self.add_service(zc, type_, name)

        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _ESPHomeListener())
        _LOGGER.info("Global mDNS listener started for %s", MDNS_SERVICE_TYPE)

    # ── Log callback ─────────────────────────────────────────────

    def _on_log_message(self, device_name: str, msg):
        device = self.devices.get(device_name)
        if not device:
            return
        line = getattr(msg, "message", str(msg))
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        device.log_buffer.append((time.time(), line))
        self._mark_online(device_name)
        self._prune_logs(device_name)

    def _prune_logs(self, device_name: str):
        device = self.devices[device_name]
        cutoff = time.time() - self.log_retention_seconds
        while device.log_buffer and device.log_buffer[0][0] < cutoff:
            device.log_buffer.popleft()

    # ── State transitions ────────────────────────────────────────

    def _mark_online(self, device_name: str):
        device = self.devices[device_name]
        now = time.time()
        if not device.online:
            device.online = True
            device.heartbeat_events.append((now, "connected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)
            asyncio.create_task(self._publish_mqtt_state(device, True))
        device.last_seen = now

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
            asyncio.create_task(self._publish_mqtt_state(device, False))
        if device.keepalive_task and not device.keepalive_task.done():
            device.keepalive_task.cancel()

    # ── Public query API ──────────────────────────────────────────

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
        ]

    def get_device_logs(self, device_name: str) -> list[dict]:
        device = self.devices.get(device_name)
        if not device:
            return []
        return [{"timestamp": ts, "message": msg} for ts, msg in device.log_buffer]

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
