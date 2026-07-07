"""Job Manager - talks to the ESPHome Device Builder's WebSocket API.

As of ESPHome 2026.6.0 the legacy REST dashboard API (POST /api/compile,
POST /api/upload) no longer exists. The new "ESPHome Device Builder"
backend is WS-first: everything goes through ws://<host>:6052/ws with a
command/response protocol. Confirmed by direct testing against the
running dashboard:

  - Connect: ws://localhost:6052/ws
  - First frame from server: {"server_version", "esphome_version", "port",
    "ha_addon", "ha_ingress", "requires_auth"}. In this HA-addon setup
    requires_auth is False, so commands can be sent immediately.
  - devices/validate {configuration}: streams {"event":"output","data":line}
    frames, terminated by {"event":"result","data":{"success","code"}}.
    Only catches YAML/schema errors, NOT C++ build errors (validation
    doesn't invoke the compiler).
  - firmware/compile {configuration}: returns immediately with
    {"result": <FirmwareJob>} (job_id/status/output/exit_code/error).
    Only one compile runs at a time; others queue.
  - firmware/get_job {job_id}: returns the current FirmwareJob snapshot.
    CONFIRMED BY TESTING: once a job reaches a terminal state
    (failed/finished), the dashboard clears "output" back to [] and
    "error" stays null even for a real C++ compiler error. Polling
    get_job AFTER completion cannot recover the error text.
  - firmware/follow_job {job_id}: streams the SAME {"event":"output",...}
    frames live while the job runs, terminated by {"event":"result",
    "data":{"status","exit_code","error"}}. This is the ONLY way to
    capture build output/errors -- it must be attached immediately after
    (or instead of relying on) firmware/compile, and the caller must
    persist the lines itself, since the ESPHome-side job clears its own
    output once terminal.

Because of this, compile jobs here are tracked with our OWN in-memory
Job objects that accumulate output via firmware/follow_job as it
streams, independent of what ESPHome's own job object still holds once
finished.

Only compile and validate are implemented. Install/flash (OTA and
USB/serial) is not implemented yet.
"""
import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger("mcp_esphome.job_manager")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _strip_ansi(line: str) -> str:
    return _ANSI_RE.sub("", line)


@dataclass
class Job:
    job_id: str
    esphome_job_id: str
    device_name: str
    job_type: str
    status: str = "running"
    exit_code: Optional[int] = None
    error: Optional[str] = None
    output: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    follow_task: Optional[asyncio.Task] = None


class JobManager:
    def __init__(self, esphome_dashboard_url: str):
        base = esphome_dashboard_url.rstrip("/")
        if base.startswith("https://"):
            self.ws_url = "wss://" + base[len("https://"):] + "/ws"
        elif base.startswith("http://"):
            self.ws_url = "ws://" + base[len("http://"):] + "/ws"
        else:
            self.ws_url = base + "/ws"
        self.jobs: dict[str, Job] = {}

    async def _connect(self):
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(self.ws_url, timeout=15)
        first = await ws.receive_json()
        if first.get("requires_auth"):
            _LOGGER.warning("ESPHome dashboard requires_auth=True — not supported")
        return session, ws

    async def validate_config(self, device_name: str) -> dict:
        """Runs devices/validate and waits for the terminal result.
        Only catches YAML/schema errors, not C++ build errors.
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
        """Starts firmware/compile, then immediately attaches
        firmware/follow_job in a background task to capture output live
        (ESPHome clears the job's own output once it terminates, so we
        must record it ourselves as it streams).
        Returns OUR OWN job_id (not ESPHome's), since our Job object is
        the durable record after ESPHome's own clears itself.
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
            esphome_job = msg["result"]
        finally:
            await ws.close()
            await session.close()

        our_job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=our_job_id,
            esphome_job_id=esphome_job["job_id"],
            device_name=device_name,
            job_type="compile",
            status=esphome_job["status"],
        )
        self.jobs[our_job_id] = job
        job.follow_task = asyncio.create_task(self._follow(job))
        _LOGGER.info("Compile started for %s: our_job_id=%s esphome_job_id=%s",
                     device_name, our_job_id, esphome_job["job_id"])
        return our_job_id

    async def _follow(self, job: Job):
        """Background task: streams firmware/follow_job and records
        every output line into our own Job object as it arrives.
        """
        session, ws = await self._connect()
        try:
            await ws.send_json({
                "command": "firmware/follow_job",
                "message_id": "1",
                "args": {"job_id": job.esphome_job_id},
            })
            while True:
                msg = await asyncio.wait_for(ws.receive_json(), timeout=600)
                event = msg.get("event")
                if event == "output":
                    job.output.append(_strip_ansi(msg.get("data", "")))
                elif event == "result":
                    data = msg.get("data", {})
                    job.status = data.get("status", "failed")
                    job.exit_code = data.get("exit_code")
                    job.error = data.get("error")
                    job.completed_at = time.time()
                    _LOGGER.info("Compile finished for %s: status=%s exit_code=%s",
                                 job.device_name, job.status, job.exit_code)
                    break
                elif "error_code" in msg:
                    job.status = "failed"
                    job.error = f"{msg.get('error_code')}: {msg.get('details')}"
                    job.completed_at = time.time()
                    break
        except Exception as err:
            job.status = "failed"
            job.error = f"follow_job connection error: {err}"
            job.completed_at = time.time()
            _LOGGER.error("follow_job failed for %s: %s", job.device_name, err)
        finally:
            await ws.close()
            await session.close()

    def get_status(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job.job_id,
            "device_name": job.device_name,
            "job_type": job.job_type,
            "status": job.status,
            "exit_code": job.exit_code,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
            "output_lines": len(job.output),
        }

    def get_full_log(self, job_id: str) -> Optional[str]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return "".join(job.output)

    def get_error_summary(self, job_id: str, context_lines: int = 30) -> Optional[str]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.status not in ("failed",) and job.exit_code in (None, 0):
            return None
        if job.error:
            return job.error
        error_idx = None
        for i, line in enumerate(job.output):
            if "error" in line.lower():
                error_idx = i
                break
        if error_idx is None:
            return "".join(job.output[-context_lines:])
        return "".join(job.output[error_idx:error_idx + context_lines])
