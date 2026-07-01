"""Device Manager - handles ESPHome device discovery, connections, logs and heartbeat history.

Architecture:
─────────────
STARTUP / INIT
  Sequential connect: ONLY devices that ESPHome dashboard reports as online
  are tried, in order. Offline devices are registered but skipped — the mDNS
  listener will pick them up when they come online.
  For each online device: keepalive_retries attempts, keepalive_ping_timeout
  seconds between each attempt. Move to next device regardless of result.
  If connected, keepalive task starts immediately in parallel.

ONLINE DETECTION (after init)
  Global mDNS listener for _esphomelib._tcp.local.
  When a device announces → _connect_device() (single attempt, no retry).

OFFLINE DETECTION
  Per-device keepalive task: ping every keepalive_interval seconds.
  keepalive_retries attempts, keepalive_ping_timeout per attempt.
  All fail → mark offline, task ends.

DISCOVERY LOOP (60s)
  Only registers NEW devices. Known devices are never re-triggered here.

ZERO CPU for offline devices.
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
        keepalive_ping_timeout_ms: int = 100,
        bearer_token: str = "",
        mqtt_host: str = "core-mosquitto",
        mqtt_port: int = 1883,
    ):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.log_retention_seconds = log_retention_hours * 3600
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
        self.keepalive_interval = keepalive_interval
        self.keepalive_retries = keepalive_retries
        self.keepalive_ping_timeout = keepalive_ping_timeout_ms / 1000.0
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
        """Read Noise PSK from ESPHome YAML config file.

        The configuration_file field from the dashboard is just the filename
        (e.g. "heizreglerv1.yaml"), not a full path. We look it up in
        ESPHOME_CONFIG_DIR (/config/esphome/).
        """
        if not configuration_file:
            return None
        # Strip any path separators — only use the filename
        filename = Path(configuration_file).name
        path = ESPHOME_CONFIG_DIR / filename
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as err:
            _LOGGER.debug("Could not read %s for noise key: %s", path, err)
            return None
        match = _NOISE_KEY_RE.search(content)
        if match:
            return match.group(1)
        _LOGGER.debug("No noise key found in %s", path)
        return None

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
                        return
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("Cannot reach ESPHome dashboard: %s", err)
            return

        configured = data.get("configured", []) if isinstance(data, dict) else []
        new_online: list[str] = []
        new_offline: list[str] = []

        for entry in configured:
            name = entry.get("name")
            if not name:
                continue

            if name in self.devices:
                # Update address/MAC for known devices only
                d = self.devices[name]
                addr = entry.get("address") or f"{name}.local"
                d.address = addr
                if entry.get("mac_address") and not d.mac_address:
                    d.mac_address = entry["mac_address"]
                continue

            address = entry.get("address") or f"{name}.local"
            configuration_file = entry.get("configuration", "")
            api_encrypted = entry.get("api_encrypted", False)
            dashboard_online = entry.get("online", False)

            device = DeviceState(name=name, address=address, ping_index=self._ping_counter)
            self._ping_counter += 1
            device.configuration_file = configuration_file
            if entry.get("mac_address"):
                device.mac_address = entry["mac_address"]
            if api_encrypted:
                psk = self._read_noise_psk(configuration_file)
                device.noise_psk = psk
                if psk:
                    _LOGGER.info("Noise PSK loaded for %s", name)
                else:
                    _LOGGER.warning("No noise PSK found for encrypted device %s (config: %s)",
                                    name, configuration_file)
            self.devices[name] = device

            if dashboard_online:
                new_online.append(name)
                _LOGGER.info("New device registered (online): %s @ %s", name, address)
            else:
                new_offline.append(name)
                _LOGGER.info("New device registered (offline): %s @ %s — skipping init", name, address)

        if new_online:
            _LOGGER.info("Sequential init for %d online device(s), %d offline skipped",
                         len(new_online), len(new_offline))
            asyncio.create_task(self._sequential_init(new_online))
        elif new_offline:
            _LOGGER.info("All %d new device(s) are offline — waiting for mDNS", len(new_offline))

    # ── Sequential init ───────────────────────────────────────────

    async def _sequential_init(self, device_names: list[str]):
        """Try connecting to ONLINE devices one by one.

        For each device: keepalive_retries attempts with keepalive_ping_timeout
        between each attempt. Move to next regardless of result.
        Keepalive task starts immediately when a device connects.
        """
        total = len(device_names)
        for i, name in enumerate(device_names):
            device = self.devices.get(name)
            if not device or device.online:
                continue

            _LOGGER.info("Init [%d/%d] %s @ %s (encrypted=%s)",
                         i + 1, total, name, device.address, device.noise_psk is not None)
            connected = False

            for attempt in range(1, self.keepalive_retries + 1):
                if device.online:
                    break
                try:
                    client = APIClient(device.address, 6053, None, noise_psk=device.noise_psk)
                    await client.connect(login=False)
                    device.client = client
                    _LOGGER.info("ONLINE (dashboard): %s (attempt %d/%d)",
                                 name, attempt, self.keepalive_retries)
                    self._mark_online(name)
                    client.subscribe_logs(lambda msg, dn=name: self._on_log_message(dn, msg))
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
                        pass
                    num = max(len(self.devices), 1)
                    stagger = (device.ping_index % num) * (self.keepalive_interval / num)
                    device.keepalive_task = asyncio.create_task(
                        self._keepalive_loop(name, client, stagger)
                    )
                    connected = True
                    break
                except Exception as err:
                    _LOGGER.info("Init connect failed [%d/%d] %s: %s",
                                 attempt, self.keepalive_retries, name, err)
                    if attempt < self.keepalive_retries:
                        await asyncio.sleep(self.keepalive_ping_timeout)

            if not connected:
                _LOGGER.info("Init: %s offline after %d attempt(s) — mDNS will reconnect",
                             name, self.keepalive_retries)

    # ── Single connect (mDNS triggered) ──────────────────────────

    async def _connect_device(self, device_name: str, source: str):
        if not HAS_AIOESPHOMEAPI:
            return
        device = self.devices.get(device_name)
        if not device or device.online or device.connecting:
            return

        device.connecting = True
        try:
            _LOGGER.info("Connecting [%s] %s @ %s (encrypted=%s) ...",
                         source, device_name, device.address, device.noise_psk is not None)
            client = APIClient(device.address, 6053, None, noise_psk=device.noise_psk)
            try:
                await client.connect(login=False)
            except Exception as err:
                _LOGGER.info("Connect failed [%s] %s: %s", source, device_name, err)
                return

            device.client = client
            _LOGGER.info("ONLINE (%s): %s", source, device_name)
            self._mark_online(device_name)
            client.subscribe_logs(lambda msg, dn=device_name: self._on_log_message(dn, msg))

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
                    pass

            device.keepalive_task = asyncio.create_task(
                self._keepalive_loop(device_name, client, 0)
            )
        finally:
            device.connecting = False

    # ── Keepalive loop ───────────────────────────────────────────

    async def _keepalive_loop(self, device_name: str, client: object, initial_stagger: float):
        device = self.devices.get(device_name)
        if not device:
            return

        if initial_stagger > 0:
            await asyncio.sleep(initial_stagger)

        while True:
            await asyncio.sleep(self.keepalive_interval)

            if not device.online:
                break

            success = False
            for attempt in range(1, self.keepalive_retries + 1):
                try:
                    await asyncio.wait_for(
                        client.device_info(),  # type: ignore[arg-type]
                        timeout=self.keepalive_ping_timeout,
                    )
                    device.last_seen = time.time()
                    _LOGGER.info("Keepalive OK: %s (attempt %d/%d)",
                                 device_name, attempt, self.keepalive_retries)
                    success = True
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    _LOGGER.info("Keepalive attempt %d/%d failed: %s — %s",
                                 attempt, self.keepalive_retries, device_name, err)
                    if attempt < self.keepalive_retries:
                        await asyncio.sleep(self.keepalive_ping_timeout)

            if not success:
                _LOGGER.info("OFFLINE (Keepalive): %s", device_name)
                self._mark_offline(device_name)
                device.client = None
                break

    # ── Global mDNS listener ──────────────────────────────────────

    async def start_mdns_listener(self):
        if not HAS_ZEROCONF:
            _LOGGER.warning("zeroconf not available — mDNS wakeup disabled")
            return

        self._loop = asyncio.get_event_loop()
        self._azeroconf = AsyncZeroconf()
        zc = self._azeroconf.zeroconf  # type: ignore[attr-defined]
        mgr = self

        class _ESPHomeListener:
            def add_service(self, zc, type_, name):
                device_name = (name
                               .replace(f".{MDNS_SERVICE_TYPE}", "")
                               .replace(f".{type_}", "")
                               .rstrip("."))
                if device_name in mgr.devices and not mgr.devices[device_name].online:
                    _LOGGER.info("mDNS announce received: %s → triggering connect", device_name)
                    asyncio.run_coroutine_threadsafe(
                        mgr._connect_device(device_name, source="ESPHome/mDNS"),
                        mgr._loop,
                    )

            def remove_service(self, zc, type_, name):
                pass

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
