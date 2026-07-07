"""Log Manager - persists ESPHome native API debug logs per device.

ROOT CAUSE FOUND AND FIXED (v0.14.4): see git history for the
get_local_timezone() singleton poisoning bug. Fixed by passing an
explicit timezone to APIClient.

v0.15.0: log-subscription task lifecycle decoupled from the fast
TCP-ping keepalive, tied only to device.esphome_reports_online instead
(see device_manager.py).

v0.15.1 (REVERTED in v0.15.2): tried using this connection's own
connect/on_stop events as a fast online/offline signal for
DeviceManager. Measured directly against a real power-loss test: the
log connection's on_stop did NOT detect the actual outage promptly. The
TCP-ping keepalive was consistently faster. Reverted; this connection's
state is used only for logging now, never for online/offline detection.

v0.15.3 (CORRECTED in v0.15.4): mDNS announces wake the log-subscription
task. v0.15.3's first version force-disconnected and reconnected the
task even while it held a healthy, actively-logging connection (any
mDNS announce would tear it down and rebuild it, confirmed in testing:
"mDNS-forced reconnect ... connection may have been stale" fired on a
connection that was in fact NOT stale). That is wrong -- a working
connection must not be interrupted. v0.15.4 fixes this: on_mdns_announce
only wakes the task if it is currently NOT connected (i.e. it's in the
RETRY_SECONDS backoff wait, or hasn't started yet). If a healthy
connection is up, the task simply blocks on the real disconnect signal
(stop_event) and mDNS announces have no effect on it at all. This is
tracked via self.connected: dict[str, bool], set True right after a
successful connect+subscribe and False again the moment that connected
phase ends for any reason.

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
    by DeviceManager based on esphome_reports_online.
  - While NOT connected (retry backoff between attempts, or waiting for
    the very first connection): waits up to RETRY_SECONDS, but an mDNS
    announce for that device (on_mdns_announce) interrupts the wait
    immediately and retries right away.
  - While CONNECTED and healthy: blocks purely on the real disconnect
    signal (aioesphomeapi's on_stop callback). mDNS announces are
    ignored during this phase -- a working connection is never torn
    down just because an announce arrived.
  - Every received log line is appended as one JSON line to
    /data/mcp_esphome/esplog_<name>.jsonl: {"ts": <epoch float>, "message": <str>}.
  - A periodic prune task rewrites each log file keeping only entries
    newer than retention_days.
  - This connection's own state is used ONLY for logging, never for
    online/offline detection (see device_manager.py's TCP-ping
    keepalive for that).
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
        self.wake_events: dict[str, asyncio.Event] = {}
        self.connected: dict[str, bool] = {}
        self.zeroconf_instance: Optional[object] = None
        self.timezone = timezone
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    def set_zeroconf_instance(self, instance: object):
        self.zeroconf_instance = instance

    def _log_file(self, device_name: str) -> Path:
        return STORAGE_DIR / f"esplog_{device_name.replace('/', '_')}.jsonl"

    def start(self, device_name: str, address: str, noise_psk: Optional[str]):
        """Starts (or leaves running) the persistent log-subscription
        task for a device. Safe to call repeatedly. This is the normal
        offline->online path (via DeviceManager.esphome_reports_online);
        the task starts fresh with the usual RETRY_SECONDS cadence.
        """
        if not HAS_AIOESPHOMEAPI:
            return
        existing = self.tasks.get(device_name)
        if existing is not None and not existing.done():
            return
        self.connected[device_name] = False
        self.tasks[device_name] = asyncio.create_task(
            self._run(device_name, address, noise_psk)
        )
        _LOGGER.info("Log subscription task started for %s", device_name)

    def stop(self, device_name: str):
        task = self.tasks.get(device_name)
        if task is not None and not task.done():
            task.cancel()
            _LOGGER.info("Log subscription task cancel requested for %s", device_name)

    def on_mdns_announce(self, device_name: str):
        """Wakes the log task ONLY if it is currently not connected
        (mid-retry-backoff, or the very first connect attempt hasn't
        happened yet). If it already holds a healthy connection, this is
        a no-op -- a working connection is never torn down just because
        an mDNS announce arrived. No-op entirely if no task exists yet
        for this device (start() via esphome_reports_online handles
        that case with its own normal cadence).
        """
        if self.connected.get(device_name):
            return
        event = self.wake_events.get(device_name)
        if event is None:
            return
        _LOGGER.info("mDNS announce for %s — not connected, waking log task early",
                     device_name)
        event.set()

    async def _run(self, device_name: str, address: str, noise_psk: Optional[str]):
        wake_event = self.wake_events.setdefault(device_name, asyncio.Event())
        while True:
            wake_event.clear()
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
                self.connected[device_name] = True
                _LOGGER.info("Log subscription connected for %s", device_name)

                # Only waits for a REAL disconnect. mDNS announces are
                # ignored here on purpose -- see module docstring v0.15.4.
                await stop_event.wait()
                _LOGGER.info("Log subscription disconnected for %s — retry in %ds",
                             device_name, RETRY_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.info("Log subscription error for %s: %s — retry in %ds",
                             device_name, err, RETRY_SECONDS)
            finally:
                self.connected[device_name] = False
                try:
                    await client.disconnect()
                except Exception:
                    pass
                _LOGGER.info("Log subscription connection closed for %s", device_name)

            try:
                await asyncio.wait_for(wake_event.wait(), timeout=RETRY_SECONDS)
                _LOGGER.info("mDNS announce for %s — skipping remaining retry wait",
                             device_name)
            except asyncio.TimeoutError:
                pass
            wake_event.clear()

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
