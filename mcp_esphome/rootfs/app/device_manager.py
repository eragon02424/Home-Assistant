"""Device Manager - handles ESPHome device discovery, connections, logs and heartbeat history.

Uses aioesphomeapi for native communication with ESPHome devices.
Maintains:
- A list of known devices (auto-discovered via the ESPHome dashboard's /devices endpoint)
- A 24h rolling log buffer per device
- A 30-day heartbeat (connect/disconnect) history per device

Noise encryption keys are read directly from each device's YAML config.
MQTT Discovery is used to auto-create Online/Offline binary_sensor entities
in HA, attached to the correct ESP device via MAC address.
"""
import asyncio
import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiohttp

try:
    from aioesphomeapi import APIClient, APIConnectionError, DeviceInfo
except ImportError:
    APIClient = None
    APIConnectionError = Exception
    DeviceInfo = None

try:
    import aiomqtt
except ImportError:
    aiomqtt = None

_LOGGER = logging.getLogger("mcp_esphome.device_manager")

STORAGE_DIR = Path("/data/mcp_esphome")
ESPHOME_CONFIG_DIR = Path("/config/esphome")
DISCOVERY_INTERVAL_SECONDS = 60

KEEPALIVE_INTERVAL = 10
KEEPALIVE_TIMEOUT  = 8
RECONNECT_INTERVAL = 15

_NOISE_KEY_RE = re.compile(
    r"api:\s*\n(?:.*\n)*?\s*encryption:\s*\n\s*key:\s*[\"']?([A-Za-z0-9+/=]+)[\"']?",
)


@dataclass
class DeviceState:
    name: str
    address: str
    configuration_file: str = ""
    noise_psk: Optional[str] = None
    mac_address: Optional[str] = None
    model: Optional[str] = None
    online: bool = False
    last_seen: Optional[float] = None
    last_disconnect: Optional[float] = None
    mqtt_discovery_published: bool = False
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=5000))
    heartbeat_events: list = field(default_factory=list)
    client: Optional[object] = None
    connect_task: Optional[asyncio.Task] = None


class DeviceManager:
    def __init__(
        self,
        esphome_dashboard_url: str,
        log_retention_hours: int,
        heartbeat_retention_days: int,
        mqtt_host: str = "core-mosquitto",
        mqtt_port: int = 1883,
        mqtt_username: str = "",
        mqtt_password: str = "",
    ):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.log_retention_seconds = log_retention_hours * 3600
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.devices: dict[str, DeviceState] = {}
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        self._load_heartbeat_history()

    # ── Persistence ──────────────────────────────────────────────

    def _heartbeat_file(self, device_name: str) -> Path:
        safe_name = device_name.replace("/", "_")
        return STORAGE_DIR / f"heartbeat_{safe_name}.json"

    def _load_heartbeat_history(self):
        if not STORAGE_DIR.exists():
            return
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

    # ── Noise PSK ───────────────────────────────────────────────────

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
        if match:
            return match.group(1)
        return None

    # ── MQTT Discovery ────────────────────────────────────────────

    async def _publish_mqtt_discovery(self, device: DeviceState):
        """Publish MQTT Discovery payload so HA auto-creates the binary_sensor
        and attaches it to the correct ESP device (matched by MAC address)."""
        if aiomqtt is None:
            _LOGGER.warning("aiomqtt not available, skipping MQTT discovery for %s", device.name)
            return

        # Use MAC as unique identifier if available, otherwise fall back to name
        identifier = device.mac_address.replace(":", "") if device.mac_address else f"esphome_{device.name}"
        unique_id = f"mcp_esphome_{identifier}_online"
        state_topic = f"mcp_esphome/{device.name}/online"
        config_topic = f"homeassistant/binary_sensor/{unique_id}/config"

        device_block: dict = {
            "name": device.name,
            "identifiers": [identifier],
        }
        if device.mac_address:
            device_block["connections"] = [["mac", device.mac_address]]
        if device.model:
            device_block["model"] = device.model

        payload = {
            "name": "Online",
            "unique_id": unique_id,
            "device_class": "connectivity",
            "state_topic": state_topic,
            "payload_on": "ON",
            "payload_off": "OFF",
            "device": device_block,
        }

        try:
            async with aiomqtt.Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.mqtt_username or None,
                password=self.mqtt_password or None,
            ) as client:
                # Publish discovery config (retained so HA picks it up after restart)
                await client.publish(
                    config_topic,
                    payload=json.dumps(payload),
                    retain=True,
                )
                # Publish initial state
                await client.publish(
                    state_topic,
                    payload="ON" if device.online else "OFF",
                    retain=True,
                )
            _LOGGER.info("MQTT Discovery published for %s (identifier: %s)", device.name, identifier)
            device.mqtt_discovery_published = True
        except Exception as err:
            _LOGGER.error("Failed to publish MQTT discovery for %s: %s", device.name, err)

    async def _publish_mqtt_state(self, device: DeviceState, online: bool):
        """Publish online/offline state update to MQTT."""
        if aiomqtt is None or not device.mqtt_discovery_published:
            return
        state_topic = f"mcp_esphome/{device.name}/online"
        try:
            async with aiomqtt.Client(
                hostname=self.mqtt_host,
                port=self.mqtt_port,
                username=self.mqtt_username or None,
                password=self.mqtt_password or None,
            ) as client:
                await client.publish(
                    state_topic,
                    payload="ON" if online else "OFF",
                    retain=True,
                )
        except Exception as err:
            _LOGGER.warning("Failed to publish MQTT state for %s: %s", device.name, err)

    # ── Discovery ────────────────────────────────────────────────

    async def run_discovery_loop(self):
        while True:
            try:
                await self._discover_devices()
            except Exception as err:
                _LOGGER.error("Discovery loop error: %s", err)
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)

    async def _discover_devices(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.esphome_dashboard_url}/devices", timeout=10) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Dashboard discovery returned status %s", resp.status)
                        return
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("Could not reach ESPHome dashboard for discovery: %s", err)
            return

        configured = data.get("configured", []) if isinstance(data, dict) else []

        for entry in configured:
            name = entry.get("name")
            configuration_file = entry.get("configuration", "")
            address = entry.get("address") or f"{name}.local"
            api_encrypted = entry.get("api_encrypted", False)
            mac_from_dashboard = entry.get("mac_address", "")
            if not name:
                continue

            if name not in self.devices:
                _LOGGER.info("New device discovered: %s (%s)", name, address)
                self.devices[name] = DeviceState(name=name, address=address)

            device = self.devices[name]
            device.address = address
            device.configuration_file = configuration_file

            # Pre-fill MAC from dashboard if available (saves one device_info() call)
            if mac_from_dashboard and not device.mac_address:
                device.mac_address = mac_from_dashboard

            if api_encrypted and device.noise_psk is None:
                device.noise_psk = self._read_noise_psk(configuration_file)
                if not device.noise_psk:
                    _LOGGER.warning(
                        "Device %s has API encryption but no key found in %s",
                        name, configuration_file,
                    )

            if device.connect_task is None or device.connect_task.done():
                device.connect_task = asyncio.create_task(self._maintain_connection(name))

    # ── Connection + Logs ───────────────────────────────────────

    async def _maintain_connection(self, device_name: str):
        if APIClient is None:
            _LOGGER.error("aioesphomeapi not available, cannot connect to %s", device_name)
            return

        device = self.devices[device_name]

        while True:
            try:
                client = APIClient(
                    device.address,
                    6053,
                    None,
                    noise_psk=device.noise_psk,
                )
                await client.connect(login=False)
                device.client = client
                self._mark_online(device_name)

                client.subscribe_logs(lambda msg, dn=device_name: self._on_log_message(dn, msg))

                first_ping = True
                while device.client is not None:
                    await asyncio.sleep(KEEPALIVE_INTERVAL)
                    try:
                        info = await asyncio.wait_for(
                            client.device_info(),
                            timeout=KEEPALIVE_TIMEOUT,
                        )
                        # On first successful ping: extract device info and publish MQTT discovery
                        if first_ping:
                            first_ping = False
                            if not device.mac_address and hasattr(info, 'mac_address'):
                                device.mac_address = info.mac_address
                            if not device.model and hasattr(info, 'model'):
                                device.model = info.model
                            if not device.mqtt_discovery_published:
                                asyncio.create_task(self._publish_mqtt_discovery(device))
                    except Exception:
                        break

            except APIConnectionError:
                pass
            except Exception as err:
                _LOGGER.debug("Connection attempt to %s failed: %s", device_name, err)

            self._mark_offline(device_name)
            device.client = None
            await asyncio.sleep(RECONNECT_INTERVAL)

    def _on_log_message(self, device_name: str, msg):
        device = self.devices.get(device_name)
        if not device:
            return
        line = getattr(msg, 'message', str(msg))
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='replace')
        device.log_buffer.append((time.time(), line))
        self._mark_online(device_name)
        self._prune_logs(device_name)

    def _prune_logs(self, device_name: str):
        device = self.devices[device_name]
        cutoff = time.time() - self.log_retention_seconds
        while device.log_buffer and device.log_buffer[0][0] < cutoff:
            device.log_buffer.popleft()

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

    # ── Public query API ──────────────────────────────────────────

    def list_devices(self) -> list[dict]:
        result = []
        for name, device in self.devices.items():
            result.append({
                "name": name,
                "address": device.address,
                "online": device.online,
                "last_seen": device.last_seen,
                "has_noise_key": device.noise_psk is not None,
                "mac_address": device.mac_address,
            })
        return result

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
        cycles = []
        current = {}
        for ts, kind in events:
            if kind == "connected":
                current = {"connected_at": ts}
            elif kind == "disconnected" and "connected_at" in current:
                current["disconnected_at"] = ts
                current["duration_seconds"] = ts - current["connected_at"]
                cycles.append(current)
                current = {}
        return cycles[-last_n_cycles:]
