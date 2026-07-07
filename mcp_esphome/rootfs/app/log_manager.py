"""Log Manager - persists ESPHome native API debug logs per device.

ROOT CAUSE FOUND AND FIXED (v0.14.4): see git history for the
get_local_timezone() singleton poisoning bug. Fixed by passing an
explicit timezone to APIClient.

v0.15.0: log-subscription task lifecycle decoupled from the fast
TCP-ping keepalive, tied only to device.esphome_reports_online instead
(see device_manager.py).

v0.15.1 (REVERTED in v0.15.2): tried using this connection's own
connect/on_stop events as a fast online/offline signal for
DeviceManager. Measured directly against a real power-loss test
(device physically unplugged/replugged, exact timestamps compared in
conversation): the log connection's on_stop did NOT detect the actual
outage at all -- it only fired ~35s later, as a side effect of the
device reconnecting and resetting the stale socket, not from
proactively noticing the drop. The TCP-ping keepalive detected both the
offline and online transitions faster in all four measured cases (1.5s,
4.2s, 5.9s, 6.3s vs. no detection at all / 21-35s for the log
connection). Reverted: this signal is unreliable for real disconnects,
only fires promptly for clean/graceful disconnects (e.g. ESPHome's own
dashboard log view appearing instant is likely from a controlled
reboot with a goodbye message, not a power loss). Keeping it caused
conflicting online/offline signals and confusing rapid flapping.

Other things verified/fixed along the way:
  - subscribe_logs(on_log, log_level=...) only delivers lines if
    log_level is explicitly set.
  - client.connect(on_stop=<async callback>) must get an async callback.
  - client.disconnect() must run in a finally block on every loop
    iteration (including on task cancellation) or the ESP's small
    max-connections limit (observed: 5) gets exhausted by abandoned
    half-open connections.
  - APIClient's zeroconf_instance is shared with the addon's own
    AsyncZeroconf (set via set_zeroconf_instance()) so log-subscription
    clients don't each spin up their own mDNS resolver.

Architecture:
  - One persistent aioesphomeapi connection per device, started/stopped
    by DeviceManager based on esphome_reports_online. Retries every
    RETRY_SECONDS on disconnect.
  - Every received log line is appended as one JSON line to
    /data/mcp_esphome/esplog_<name>.jsonl: {"ts": <epoch float>, "message": <str>}.
  - A periodic prune task rewrites each log file keeping only entries
    newer than retention_days.
  - This connection's own state is used ONLY for logging, not for
    online/offline detection. Online/offline detection is purely the
    TCP-ping keepalive in device_manager.py.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

try:
    from aioesphomeapi import APIClient, LogLevel
    HAS_AIOESPHOMEAPI = True
except ImportError:
    HAS_AIOESPHOMEAPI = False

_LOGGER = logging.getLogger("mcp_esphome.log_manager")

STORAGE_DIR = Path("/data/mcp_esphome")
RETRY_SECONDS = 15
PRUNE_INTERVAL_SECONDS = 6 * 3600

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


class LogManager:
    def __init__(self, retention_days: int, timezone: Optional[str] = None):
        self.retention_seconds = retention_days * 86400
        self.tasks: dict[str, asyncio.Task] = {}
        self.zeroconf_instance: Optional[object] = None
        self.timezone = timezone
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    def set_zeroconf_instance(self, instance: object):
        self.zeroconf_instance = instance

    def _log_file(self, device_name: str) -> Path:
        return STORAGE_DIR / f"esplog_{device_name.replace('/', '_')}.jsonl"

    def start(self, device_name: str, address: str, noise_psk: Optional[str]):
        """Starts (or leaves running) the persistent log-subscription
        task for a device. Safe to call repeatedly.
        """
        if not HAS_AIOESPHOMEAPI:
            return
        existing = self.tasks.get(device_name)
        if existing is not None and not existing.done():
            return
        self.tasks[device_name] = asyncio.create_task(
            self._run(device_name, address, noise_psk)
        )
        _LOGGER.info("Log subscription task started for %s", device_name)

    def stop(self, device_name: str):
        task = self.tasks.get(device_name)
        if task is not None and not task.done():
            task.cancel()
            _LOGGER.info("Log subscription task cancel requested for %s", device_name)

    async def _run(self, device_name: str, address: str, noise_psk: Optional[str]):
        while True:
            client = APIClient(
                address, 6053, None,
                noise_psk=noise_psk,
                zeroconf_instance=self.zeroconf_instance,
                timezone=self.timezone,
            )
            stop_event = asyncio.Event()

            async def on_stop(expected, ev=stop_event):
                ev.set()

            try:
                await client.connect(login=False, on_stop=on_stop)

                def on_log(msg, dn=device_name):
                    line = msg.message
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    self._append(dn, _strip_ansi(line))

                client.subscribe_logs(on_log, log_level=LogLevel.LOG_LEVEL_VERY_VERBOSE)
                _LOGGER.info("Log subscription connected for %s", device_name)
                await stop_event.wait()
                _LOGGER.info("Log subscription disconnected for %s — retry in %ds",
                             device_name, RETRY_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.info("Log subscription error for %s: %s — retry in %ds",
                             device_name, err, RETRY_SECONDS)
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                _LOGGER.info("Log subscription connection closed for %s", device_name)

            await asyncio.sleep(RETRY_SECONDS)

    def _append(self, device_name: str, line: str):
        try:
            with open(self._log_file(device_name), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.time(), "message": line}) + "\n")
        except Exception as err:
            _LOGGER.error("Failed to write log line for %s: %s", device_name, err)

    async def run_prune_loop(self):
        while True:
            await asyncio.sleep(PRUNE_INTERVAL_SECONDS)
            self.prune_all()

    def prune_all(self):
        cutoff = time.time() - self.retention_seconds
        for path in STORAGE_DIR.glob("esplog_*.jsonl"):
            try:
                kept = []
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("ts", 0) >= cutoff:
                            kept.append(line)
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(kept)
            except Exception as err:
                _LOGGER.error("Failed to prune %s: %s", path, err)

    def get_recent(self, device_name: str, n: int = 100) -> list[dict]:
        path = self._log_file(device_name)
        if not path.exists():
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        result = []
        for line in lines[-n:]:
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result

    def get_range(self, device_name: str, since_seconds: float) -> list[dict]:
        path = self._log_file(device_name)
        if not path.exists():
            return []
        cutoff = time.time() - since_seconds
        result = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("ts", 0) >= cutoff:
                    result.append(obj)
        return result
