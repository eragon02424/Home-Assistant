"""Job Manager - talks to the ESPHome Device Builder's WebSocket API.

As of ESPHome 2026.6.0 the legacy REST dashboard API (POST /api/compile,
POST /api/upload) no longer exists. The new "ESPHome Device Builder"
backend is WS-first: everything goes through ws://<host>:6052/ws with a
command/response protocol. Confirmed by direct testing against the
running dashboard (see conversation history):

  - Connect: ws://localhost:6052/ws
  - First frame from server: {"server_version", "esphome_version", "port",
    "ha_addon", "ha_ingress", "requires_auth"}. In this HA-addon setup
    requires_auth is False (no username/password configured for the
    add-on flavor), so commands can be sent immediately.
  - devices/validate {configuration}: streams {"message_id","event":"output",
    "data": <line>} frames, terminated by {"event":"result",
    "data":{"success": bool, "code": int}}. Errors (e.g. bad YAML, missing
    file) surface as additional "output" lines before the terminal result.
  - firmware/compile {configuration}: NOT streamed. Returns immediately
    with {"message_id","result": <FirmwareJob>} where FirmwareJob has
    job_id/status/output/exit_code/error. status starts "queued" or
    "running" (only one compile runs at a time; others queue).
  - firmware/get_job {job_id}: returns the current FirmwareJob snapshot,
    including the full accumulated output list so far. Poll this
    repeatedly until status is no longer "running"/"queued"/"pending".

Only compile and validate are implemented here (compile never flashes).
Install/flash (OTA and USB/serial) is intentionally NOT implemented yet.
"""
import asyncio
import logging
import re
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger("mcp_esphome.job_manager")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


class JobManager:
    def __init__(self, esphome_dashboard_url: str):
        # ws:// version of the dashboard's http(s):// URL
        base = esphome_dashboard_url.rstrip("/")
        if base.startswith("https://"):
            self.ws_url = "wss://" + base[len("https://"):] + "/ws"
        elif base.startswith("http://"):
            self.ws_url = "ws://" + base[len("http://"):] + "/ws"
        else:
            self.ws_url = base + "/ws"

    async def _connect(self):
        """Opens a fresh WS connection and consumes the initial
        ServerInfoMessage. Caller is responsible for closing.
        """
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(self.ws_url, timeout=15)
        first = await ws.receive_json()
        if first.get("requires_auth"):
            # Not implemented: this HA-addon deployment has no
            # username/password configured, so this path is untested.
            _LOGGER.warning("ESPHome dashboard requires_auth=True — not supported")
        return session, ws

    async def validate_config(self, device_name: str) -> dict:
        """Runs devices/validate and waits for the terminal result.
        Returns {"success": bool, "code": int, "output": [lines]}.
        """
        configuration = f"{device_name}.yaml"
        session, ws = await self._connect()
        output_lines: list[str] = []
        try:
            await ws.send_json({
                "command": "devices/validate",
                "message_id": "1",
                "args": {"configuration": configuration},
            })
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=60)
                event = msg.get("event")
                if event == "output":
                    output_lines.append(_strip_ansi(msg.get("data", "")))
                elif event == "result":
                    data = msg.get("data", {})
                    return {
                        "success": data.get("success", False),
                        "code": data.get("code"),
                        "output": output_lines,
                    }
                elif "error_code" in msg:
                    return {
                        "success": False,
                        "code": None,
                        "output": output_lines + [f"ERROR: {msg.get('details', msg.get('error_code'))}"],
                    }
        finally:
            await ws.close()
            await session.close()

    async def start_compile(self, device_name: str) -> str:
        """Queues a compile job (never flashes). Returns the ESPHome
        dashboard's own job_id — we don't mint our own.
        """
        configuration = f"{device_name}.yaml"
        session, ws = await self._connect()
        try:
            await ws.send_json({
                "command": "firmware/compile",
                "message_id": "1",
                "args": {"configuration": configuration},
            })
            msg = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if "error_code" in msg:
                raise RuntimeError(f"{msg.get('error_code')}: {msg.get('details')}")
            job = msg["result"]
            _LOGGER.info("Compile queued for %s: job_id=%s status=%s",
                         device_name, job["job_id"], job["status"])
            return job["job_id"]
        finally:
            await ws.close()
            await session.close()

    async def get_job(self, job_id: str) -> Optional[dict]:
        """Returns the current FirmwareJob snapshot (status, output,
        exit_code, error), or None if the job_id is unknown.
        """
        session, ws = await self._connect()
        try:
            await ws.send_json({
                "command": "firmware/get_job",
                "message_id": "1",
                "args": {"job_id": job_id},
            })
            msg = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if "error_code" in msg:
                return None
            return msg["result"]
        finally:
            await ws.close()
            await session.close()

    async def get_status(self, job_id: str) -> Optional[dict]:
        job = await self.get_job(job_id)
        if job is None:
            return None
        return {
            "job_id": job["job_id"],
            "configuration": job["configuration"],
            "job_type": job["job_type"],
            "status": job["status"],
            "exit_code": job["exit_code"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
        }

    async def get_full_log(self, job_id: str) -> Optional[str]:
        job = await self.get_job(job_id)
        if job is None:
            return None
        return "".join(_strip_ansi(line) for line in job["output"])

    async def get_error_summary(self, job_id: str, context_lines: int = 30) -> Optional[str]:
        job = await self.get_job(job_id)
        if job is None:
            return None
        lines = [_strip_ansi(line) for line in job["output"]]
        if job.get("error"):
            return job["error"]
        error_idx = None
        for i, line in enumerate(lines):
            if "error" in line.lower():
                error_idx = i
                break
        if error_idx is None:
            return "".join(lines[-context_lines:])
        return "".join(lines[error_idx:error_idx + context_lines])
