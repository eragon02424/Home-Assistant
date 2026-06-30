"""Device Manager - handles ESPHome device discovery, connections, logs and heartbeat history.

Uses aioesphomeapi for native communication with ESPHome devices.
Maintains:
- A list of known devices (auto-discovered via the ESPHome dashboard's /devices endpoint)
- A 24h rolling log buffer per device
- A 30-day heartbeat (connect/disconnect) history per device

Noise encryption keys are read directly from each device's YAML config
(api.encryption.key), which is where ESPHome puts them by default -
NOT from secrets.yaml (which only holds wifi credentials in this setup).
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
    from aioesphomeapi import APIClient, APIConnectionError
except ImportError:
    APIClient = None
    APIConnectionError = Exception

_LOGGER = logging.getLogger("mcp_esphome.device_manager")

STORAGE_DIR = Path("/data/mcp_esphome")
ESPHOME_CONFIG_DIR = Path("/config/esphome")
DISCOVERY_INTERVAL_SECONDS = 60

# Matches:
# api:
#   encryption:
#     key: "....."
_NOISE_KEY_RE = re.compile(
    r"api:\s*\n(?:.*\n)*?\s*encryption:\s*\n\s*key:\s*[\"']?([A-Za-z0-9+/=]+)[\"']?",
)


@dataclass
class DeviceState:
    name: str
    address: str
    configuration_file: str = ""
    noise_psk: Optional[str] = None
    online: bool = False
    last_seen: Optional[float] = None
    last_disconnect: Optional[float] = None
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=5000))
    heartbeat_events: list = field(default_factory=list)  # [(timestamp, "connected"/"disconnected")]
    client: Optional[object] = None
    connect_task: Optional[asyncio.Task] = None


class DeviceManager:
    def __init__(self, esphome_dashboard_url: str, log_retention_hours: int, heartbeat_retention_days: int):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.log_retention_seconds = log_retention_hours * 3600
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
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

    # ── Noise PSK extraction ─────────────────────────────────────

    def _read_noise_psk(self, configuration_file: str) -> Optional[str]:
        """Read the Noise encryption key directly from the device's YAML file.

        ESPHome puts this under api.encryption.key by default - it is NOT
        moved to secrets.yaml automatically. We read it with a regex instead
        of a full YAML parser to avoid choking on ESPHome's custom YAML tags
        (!secret, !lambda, etc.) that a plain yaml.safe_load() can't handle.
        """
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

    # ── Discovery ────────────────────────────────────────────────

    async def run_discovery_loop(self):
        """Periodically discover ESPHome devices from the dashboard and ensure connections."""
        while True:
            try:
                await self._discover_devices()
            except Exception as err:
                _LOGGER.error("Discovery loop error: %s", err)
            await asyncio.sleep(DISCOVERY_INTERVAL_SECONDS)

    async def _discover_devices(self):
        """Query the ESPHome dashboard for known devices and ensure we have connections."""
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
            if not name:
                continue

            if name not in self.devices:
                _LOGGER.info("New device discovered: %s (%s)", name, address)
                self.devices[name] = DeviceState(name=name, address=address)

            device = self.devices[name]
            device.address = address
            device.configuration_file = configuration_file

            if api_encrypted and device.noise_psk is None:
                device.noise_psk = self._read_noise_psk(configuration_file)
                if device.noise_psk:
                    _LOGGER.debug("Noise PSK loaded for %s", name)
                else:
                    _LOGGER.warning(
                        "Device %s has API encryption enabled but no key found in %s "
                        "(may be in secrets.yaml or a non-standard location)",
                        name, configuration_file,
                    )

            if device.connect_task is None or device.connect_task.done():
                device.connect_task = asyncio.create_task(self._maintain_connection(name))

        # Note: devices that disappear from dashboard config are NOT removed automatically
        # (heartbeat history should persist). They'll just show as long-offline.

    # ── Connection + Logs ────────────────────────────────────────

    async def _maintain_connection(self, device_name: str):
        """Keep a persistent connection to a device, handling reconnects for sleepy devices."""
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

                # Wait until disconnected
                while device.client is not None:
                    await asyncio.sleep(5)
                    try:
                        await client.device_info()
                    except Exception:
                        break

            except APIConnectionError:
                pass
            except Exception as err:
                _LOGGER.debug("Connection attempt to %s failed: %s", device_name, err)

            self._mark_offline(device_name)
            device.client = None
            await asyncio.sleep(15)  # retry interval for sleepy/offline devices

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

    def _mark_online(self, device_name: str):
        device = self.devices[device_name]
        now = time.time()
        if not device.online:
            device.online = True
            device.heartbeat_events.append((now, "connected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)
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

    # ── Public query API (used by api_routes.py) ────────────────

    def list_devices(self) -> list[dict]:
        result = []
        for name, device in self.devices.items():
            result.append({
                "name": name,
                "address": device.address,
                "online": device.online,
                "last_seen": device.last_seen,
                "has_noise_key": device.noise_psk is not None,
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
