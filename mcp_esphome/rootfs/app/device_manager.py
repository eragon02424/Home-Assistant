"""Device Manager - handles ESPHome device discovery and heartbeat history.

Architecture (v0.9.0):
──────────────────────
DISCOVERY (at startup, then every DISCOVERY_INTERVAL_SECONDS)
  Fetch /devices. For every device not yet known: register it (address,
  mac, psk) and start ONE keepalive task that runs forever for the
  lifetime of the addon. Also updates device.esphome_reports_online from
  ESPHome's own "state" field on every cycle for already-known devices.

PER-DEVICE KEEPALIVE TASK (never dies once started)
  Loop forever:
    - TCP ping port 6053 (keepalive_retries attempts, keepalive_ping_timeout each)
    - success -> mark online, backoff_multiplier resets to 1, wait base interval
    - failure -> mark offline. Wait time depends on what ESPHome itself
                 currently reports for this device (esphome_reports_online,
                 refreshed every DISCOVERY_INTERVAL_SECONDS by discovery):
                   * ESPHome still says online -> our ping just missed it,
                     wait only the base interval, backoff stays at 1.
                   * ESPHome says offline -> apply exponential backoff:
                     wait interval*backoff_multiplier, then double
                     (capped at cap_multiplier).
  The wait is interruptible: an mDNS announce for this device sets an
  asyncio.Event which wakes the task immediately, resets backoff to 1,
  and triggers an immediate re-ping.

BACKOFF CAP
  cap_multiplier is the smallest power of two such that
  keepalive_interval * cap_multiplier >= keepalive_max_backoff_seconds.

mDNS LISTENER (global)
  add_service/update_service fires for every announce. If the device is
  known, its wake_event is set via loop.call_soon_threadsafe.

PERSISTENCE / HISTORY
  Every state transition (connected/disconnected) is appended to
  device.heartbeat_events and immediately written to
  /data/mcp_esphome/heartbeat_<name>.json. This file is the durable
  source of truth and is reloaded on addon/HA restart via
  _load_heartbeat_history(), so online/offline history survives restarts.

  get_online_offline_history() derives the last N online periods and the
  last N offline periods from this persisted event list (used by the
  /history API endpoint).

  list_devices() additionally exposes, per device, the duration of the
  last COMPLETED online period and the last COMPLETED offline period
  (last_online_duration_seconds / last_offline_duration_seconds), derived
  from the same persisted events. These feed the "Zuletzt Online" /
  "Zuletzt Offline" sensors in the ESPHome LiveState integration and are
  correct across restarts since they come from the persisted file, not
  from in-memory-only state.

Every TCP ping attempt logs its duration in ms.

ZERO aioesphomeapi.
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
DISCOVERY_INTERVAL_SECONDS = 120
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


def compute_backoff_cap_multiplier(interval: int, max_backoff_seconds: int) -> int:
    """Smallest power of two k such that interval * k >= max_backoff_seconds.
    Always rounds UP to the next power of two, never down.
    """
    if interval <= 0 or max_backoff_seconds <= interval:
        return 1
    k = 1
    while interval * k < max_backoff_seconds:
        k *= 2
    return k


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
    backoff_multiplier: int = 1
    wake_event: Optional[asyncio.Event] = None
    esphome_reports_online: bool = True


class DeviceManager:
    def __init__(
        self,
        esphome_dashboard_url: str,
        log_retention_hours: int,
        heartbeat_retention_days: int,
        keepalive_interval: int = 10,
        keepalive_retries: int = 5,
        keepalive_ping_timeout_ms: int = 500,
        keepalive_max_backoff_seconds: int = 21600,
        bearer_token: str = "",
    ):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.heartbeat_retention_seconds = heartbeat_retention_days * 86400
        self.keepalive_interval = keepalive_interval
        self.keepalive_retries = keepalive_retries
        self.keepalive_ping_timeout = keepalive_ping_timeout_ms / 1000.0
        self.backoff_cap_multiplier = compute_backoff_cap_multiplier(
            keepalive_interval, keepalive_max_backoff_seconds
        )
        _LOGGER.info(
            "Backoff cap: multiplier=%d -> max wait=%ds (requested max_backoff=%ds)",
            self.backoff_cap_multiplier,
            keepalive_interval * self.backoff_cap_multiplier,
            keepalive_max_backoff_seconds,
        )
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
                _LOGGER.info("Loaded %d heartbeat event(s) for %s from disk",
                             len(events), device_name)
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

    async def _tcp_ping(self, address: str, port: int = 6053) -> tuple[bool, float]:
        """Returns (success, duration_ms)."""
        start = time.monotonic()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(address, port),
                timeout=self.keepalive_ping_timeout,
            )
            duration_ms = (time.monotonic() - start) * 1000
            writer.close()
            await writer.wait_closed()
            return True, duration_ms
        except Exception:
            duration_ms = (time.monotonic() - start) * 1000
            return False, duration_ms

    # ── Discovery ─────────────────────────────────────────────

    async def run_initial_discovery(self):
        try:
            await self._discover_new_devices()
        except Exception as err:
            _LOGGER.error("Initial discovery error: %s", err)

    async def run_discovery_loop(self):
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
                        _LOGGER.warning("Dashboard /devices returned HTTP %s", resp.status)
                        return
                    data = await resp.json()
        except Exception as err:
            _LOGGER.warning("Cannot reach ESPHome dashboard: %s", err)
            return

        configured = data.get("configured", []) if isinstance(data, dict) else []
        _LOGGER.info("Discovery cycle: %d device(s) configured in ESPHome", len(configured))

        for entry in configured:
            name = entry.get("name")
            if not name:
                continue

            address = entry.get("address") or f"{name}.local"
            mac = entry.get("mac_address", "")
            configuration_file = entry.get("configuration", "")
            api_encrypted = entry.get("api_encrypted", False)
            esphome_online = entry.get("state") == "online"

            if name in self.devices:
                device = self.devices[name]
                device.address = address
                if mac and not device.mac_address:
                    device.mac_address = mac

                if device.esphome_reports_online != esphome_online:
                    _LOGGER.info(
                        "ESPHome state changed for %s: %s -> %s",
                        name,
                        "online" if device.esphome_reports_online else "offline",
                        "online" if esphome_online else "offline",
                    )
                device.esphome_reports_online = esphome_online

                if not device.initialized:
                    device.configuration_file = configuration_file
                    psk = self._read_noise_psk(configuration_file)
                    device.noise_psk = psk
                    device.initialized = True
                    if psk:
                        _LOGGER.info("Noise PSK loaded (stub) for %s", name)
                    elif api_encrypted:
                        _LOGGER.warning("No noise PSK for encrypted device %s", name)

                self._ensure_keepalive_task(name)
                continue

            psk = self._read_noise_psk(configuration_file)
            device = DeviceState(
                name=name, address=address, ping_index=self._ping_counter,
                configuration_file=configuration_file, noise_psk=psk,
                mac_address=mac or None, initialized=True,
                esphome_reports_online=esphome_online,
            )
            self._ping_counter += 1

            if psk:
                _LOGGER.info("Noise PSK loaded for %s", name)
            elif api_encrypted:
                _LOGGER.warning("No noise PSK for encrypted device %s (config: %s)",
                                name, configuration_file)

            self.devices[name] = device
            _LOGGER.info("New device discovered: %s @ %s (esphome_state=%s)",
                         name, address, "online" if esphome_online else "offline")
            self._ensure_keepalive_task(name)

    def _ensure_keepalive_task(self, device_name: str):
        device = self.devices.get(device_name)
        if not device or not device.initialized:
            return
        if device.keepalive_task is not None and not device.keepalive_task.done():
            return
        if device.wake_event is None:
            device.wake_event = asyncio.Event()
        _LOGGER.info("Starting permanent keepalive task for %s", device_name)
        device.keepalive_task = asyncio.create_task(self._keepalive_loop(device_name))

    # ── Keepalive (permanent, per-device, conditional exponential backoff) ─

    async def _keepalive_loop(self, device_name: str):
        device = self.devices.get(device_name)
        if not device:
            return

        _LOGGER.info("Keepalive loop started for %s", device_name)

        while True:
            success = False
            for attempt in range(1, self.keepalive_retries + 1):
                ok, duration_ms = await self._tcp_ping(device.address)
                if ok:
                    success = True
                    _LOGGER.info("Ping OK: %s (attempt %d/%d) %.1fms",
                                 device_name, attempt, self.keepalive_retries, duration_ms)
                    break
                _LOGGER.info("Ping failed %d/%d: %s %.1fms",
                             attempt, self.keepalive_retries, device_name, duration_ms)
                if attempt < self.keepalive_retries:
                    await asyncio.sleep(self.keepalive_ping_timeout)

            if success:
                device.last_seen = time.time()
                self._mark_online(device_name)
                if device.backoff_multiplier != 1:
                    _LOGGER.info("Backoff reset for %s (was x%d)",
                                 device_name, device.backoff_multiplier)
                device.backoff_multiplier = 1
                wait_seconds = self.keepalive_interval
            else:
                self._mark_offline(device_name)
                if device.esphome_reports_online:
                    wait_seconds = self.keepalive_interval
                    _LOGGER.info(
                        "Ping failed for %s but ESPHome still reports online — "
                        "staying at base interval %ds, no backoff growth",
                        device_name, wait_seconds
                    )
                else:
                    wait_seconds = self.keepalive_interval * device.backoff_multiplier
                    next_multiplier = min(
                        device.backoff_multiplier * 2, self.backoff_cap_multiplier
                    )
                    _LOGGER.info(
                        "ESPHome reports %s offline — backoff wait %ds "
                        "(backoff x%d -> next x%d)",
                        device_name, wait_seconds, device.backoff_multiplier, next_multiplier
                    )
                    device.backoff_multiplier = next_multiplier

            device.wake_event.clear()
            try:
                await asyncio.wait_for(device.wake_event.wait(), timeout=wait_seconds)
                _LOGGER.info("Keepalive for %s woken early by mDNS — backoff reset", device_name)
                device.backoff_multiplier = 1
            except asyncio.TimeoutError:
                pass

    # ── mDNS listener ─────────────────────────────────────────

    def _on_mdns_event(self, device_name: str, kind: str):
        device = self.devices.get(device_name)
        if not device:
            _LOGGER.info("mDNS %s [%s]: device not registered yet, ignoring", kind, device_name)
            return
        if not device.initialized:
            _LOGGER.info("mDNS %s [%s]: not yet initialized, ignoring", kind, device_name)
            return
        if device.wake_event is None:
            _LOGGER.info("mDNS %s [%s]: no wake_event yet (task not started), ignoring",
                         kind, device_name)
            return
        _LOGGER.info("mDNS %s fired: %s → waking keepalive task", kind, device_name)
        device.wake_event.set()

    async def start_mdns_listener(self):
        if not HAS_ZEROCONF:
            _LOGGER.warning("zeroconf not available — mDNS wakeup disabled")
            return

        self._loop = asyncio.get_event_loop()
        self._azeroconf = AsyncZeroconf()
        zc = self._azeroconf.zeroconf  # type: ignore[attr-defined]
        mgr = self

        def _name_to_device(name: str, type_: str) -> str:
            return (name
                    .replace(f".{MDNS_SERVICE_TYPE}", "")
                    .replace(f".{type_}", "")
                    .rstrip("."))

        class _Listener:
            def add_service(self, zc, type_, name):
                device_name = _name_to_device(name, type_)
                mgr._loop.call_soon_threadsafe(mgr._on_mdns_event, device_name, "add_service")

            def remove_service(self, zc, type_, name):
                device_name = _name_to_device(name, type_)
                _LOGGER.debug("mDNS remove_service fired: %s (ignored)", device_name)

            def update_service(self, zc, type_, name):
                device_name = _name_to_device(name, type_)
                mgr._loop.call_soon_threadsafe(mgr._on_mdns_event, device_name, "update_service")

        ServiceBrowser(zc, MDNS_SERVICE_TYPE, _Listener())
        _LOGGER.info("Global mDNS listener started")

    # ── State transitions ─────────────────────────────────────

    def _mark_online(self, device_name: str):
        device = self.devices[device_name]
        now = time.time()
        if not device.online:
            device.online = True
            device.heartbeat_events.append((now, "connected"))
            self._prune_heartbeat(device_name)
            self._save_heartbeat_history(device_name)
            _LOGGER.info("State → ONLINE: %s", device_name)
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
            _LOGGER.info("State → OFFLINE: %s", device_name)

    # ── Public query API ──────────────────────────────────────

    def get_bearer_token(self) -> str:
        return self.bearer_token

    def _last_completed_periods(self, device: DeviceState) -> tuple[Optional[dict], Optional[dict]]:
        """Scans heartbeat_events once and returns
        (last_completed_online_period, last_completed_offline_period),
        each as {"duration_seconds": ..., "ended_at": ...} or None if no
        such completed period exists yet. Derived purely from the
        persisted event list, so this is correct immediately after a
        restart, before any new ping has run.
        """
        last_online: Optional[dict] = None
        last_offline: Optional[dict] = None
        pending_start: Optional[float] = None
        pending_kind: Optional[str] = None

        for ts, kind in device.heartbeat_events:
            if kind == "connected":
                if pending_kind == "disconnected" and pending_start is not None:
                    last_offline = {"duration_seconds": ts - pending_start, "ended_at": ts}
                pending_start = ts
                pending_kind = "connected"
            elif kind == "disconnected":
                if pending_kind == "connected" and pending_start is not None:
                    last_online = {"duration_seconds": ts - pending_start, "ended_at": ts}
                pending_start = ts
                pending_kind = "disconnected"

        return last_online, last_offline

    def list_devices(self) -> list[dict]:
        result = []
        for name, device in self.devices.items():
            if not device.initialized:
                continue
            last_online, last_offline = self._last_completed_periods(device)
            result.append({
                "name": name,
                "address": device.address,
                "online": device.online,
                "last_seen": device.last_seen,
                "has_noise_key": device.noise_psk is not None,
                "mac_address": device.mac_address,
                "backoff_multiplier": device.backoff_multiplier,
                "esphome_reports_online": device.esphome_reports_online,
                "last_online_duration_seconds": last_online["duration_seconds"] if last_online else None,
                "last_online_ended_at": last_online["ended_at"] if last_online else None,
                "last_offline_duration_seconds": last_offline["duration_seconds"] if last_offline else None,
                "last_offline_ended_at": last_offline["ended_at"] if last_offline else None,
            })
        return result

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

    def get_online_offline_history(self, device_name: str, last_n: int = 10) -> Optional[dict]:
        """Derives the last N completed online periods and the last N
        completed offline periods from the persisted heartbeat_events.
        Survives HA/addon restarts because heartbeat_events is loaded from
        /data/mcp_esphome/heartbeat_<name>.json on startup.
        """
        device = self.devices.get(device_name)
        if not device:
            return None

        events = device.heartbeat_events
        online_periods: list[dict] = []
        offline_periods: list[dict] = []
        pending_start: Optional[float] = None
        pending_kind: Optional[str] = None

        for ts, kind in events:
            if kind == "connected":
                if pending_kind == "disconnected" and pending_start is not None:
                    offline_periods.append({
                        "start": pending_start,
                        "end": ts,
                        "duration_seconds": ts - pending_start,
                    })
                pending_start = ts
                pending_kind = "connected"
            elif kind == "disconnected":
                if pending_kind == "connected" and pending_start is not None:
                    online_periods.append({
                        "start": pending_start,
                        "end": ts,
                        "duration_seconds": ts - pending_start,
                    })
                pending_start = ts
                pending_kind = "disconnected"

        return {
            "online_periods": online_periods[-last_n:],
            "offline_periods": offline_periods[-last_n:],
            "current_state": "online" if device.online else "offline",
            "current_state_since": pending_start,
        }
