"""Log Manager - persists ESPHome native API debug logs per device.

Verified by direct testing before implementation:
  - aioesphomeapi.subscribe_logs(on_log, log_level=...) only delivers
    lines if log_level is explicitly set (log_level=None yields zero
    lines, even over 60s of observation).
  - client.connect(on_stop=<async callback>) fires on_stop(expected: bool)
    exactly when the connection drops. on_stop must be an async function
    (Callable[[bool], Coroutine]), not a plain sync callback.
  - No connection resets or interference were observed between this
    log-subscription connection and the addon's separate TCP-ping
    keepalive connections to the same device over a 30-40s test window.

Architecture:
  - One persistent aioesphomeapi connection per device, started when
    DeviceManager marks a device online, stopped (task cancelled) when
    marked offline. On unexpected disconnect the connection retries
    every RETRY_SECONDS until the task is cancelled.
  - CRITICAL: every attempt explicitly calls client.disconnect() in a
    finally block, regardless of whether the loop iteration ended via
    normal disconnect, an exception, or task cancellation (stop()).
    Without this, cancelling the task (as stop() does on every offline
    transition) abandons the connection without a clean close — the ESP
    only has a small max-connections limit (observed: 5), and repeated
    online/offline flapping without a clean disconnect exhausts it
    within minutes, causing "Max connections, rejecting" on the device
    for ALL clients (including our own TCP-ping keepalive and ESPHome
    itself). This was found and fixed after observing exactly that
    warning while testing.
  - Every received log line is appended as one JSON line to
    /data/mcp_esphome/esplog_<name>.jsonl: {"ts": <epoch float>, "message": <str>}.
  - A periodic prune task rewrites each log file keeping only entries
    newer than retention_days.
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
    def __init__(self, retention_days: int):
        self.retention_seconds = retention_days * 86400
        self.tasks: dict[str, asyncio.Task] = {}
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)

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
            client = APIClient(address, 6053, None, noise_psk=noise_psk)
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
                # Always close cleanly, whether we exit via normal
                # disconnect, an exception, or task cancellation from
                # stop(). Skipping this leaks a connection slot on the
                # ESP (max-connections limit observed at 5).
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
