"""Job Manager - talks to the ESPHome Device Builder's WebSocket API.

As of ESPHome 2026.6.0 the legacy REST dashboard API no longer exists.
The new "ESPHome Device Builder" backend is WS-first:
ws://<host>:6052/ws with a command/response protocol PLUS a real-time
event bus.

  - Connect: first frame from server is {"server_version",
    "esphome_version", "port", "ha_addon", "ha_ingress", "requires_auth"}.
    requires_auth is False in this HA-addon setup.
  - devices/validate {configuration}: streams output, terminated by a
    result event. Only catches YAML/schema errors, not C++ build errors.
  - firmware/compile {configuration}: returns immediately with a
    FirmwareJob (job_id/status/...).
  - firmware/install {configuration, port}: same shape, but once the
    compile phase succeeds ESPHome AUTOMATICALLY chains a SEPARATE
    upload job (its own new job_id) that performs the actual flash.
  - firmware/get_job {job_id}: CONFIRMED that once a job reaches a
    terminal state, ESPHome clears its own "output" back to [].
  - firmware/follow_job {job_id}: streams one job's output live, but
    you have to already know its job_id -- no use for discovering the
    chained upload job.

DISCOVERING THE CHAINED UPLOAD JOB -- v0.17.0 approach:
  Earlier versions (0.16.x) discovered the chained job_id by polling the
  ESPHome addon's own Supervisor service logs for a
  "Starting job <id>: upload <configuration>" line. That required
  hassio_api + hassio_role: manager permissions on THIS addon (to read
  another addon's logs), which is more privilege than strictly needed.

  The backend's own architecture doc (device-builder/CLAUDE.md) states:
  "WS-first API. Real-time updates are the default -- clients
  subscribe_events once and get pushes. Stateful lists ship through
  subscribe_events, not a list_* WS command." Testing confirmed this
  broadcasts job_queued / job_started / job_output / job_completed
  events for EVERY job on the server over a single subscribed
  connection -- including the auto-chained upload job, the moment it's
  queued, complete with its own new job_id and the "configuration" and
  "job_type" fields needed to match it to our pending install.

  This removes the need for hassio_api/manager entirely: one persistent
  subscribe_events connection, held for the JobManager's lifetime,
  replaces the Supervisor-log-polling hack. See _run_event_listener().
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
    device_name: str
    job_type: str  # "compile" or "install"
    configuration: str
    status: str = "running"
    exit_code: Optional[int] = None
    error: Optional[str] = None
    output: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    # For install jobs: index into `output` where the upload/flash
    # phase's own lines start. None until that phase's job_queued
    # event arrives.
    flash_output_start_index: Optional[int] = None


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
        # Maps ESPHome's own job_id -> our Job object, for BOTH the
        # compile job_id and (once discovered) the chained upload job_id.
        self._esphome_job_map: dict[str, Job] = {}
        # Installs whose compile phase is still running / whose chained
        # upload job hasn't been seen yet, keyed by configuration
        # filename (e.g. "heizreglerv1.yaml"), since that's what the
        # chained job's job_queued event carries.
        self._pending_installs: dict[str, Job] = {}
        self._event_task: Optional[asyncio.Task] = None
        self._event_session: Optional[aiohttp.ClientSession] = None
        self._event_ws: Optional[object] = None

    async def start(self):
        """Starts the persistent subscribe_events listener. Call once
        at addon startup.
        """
        self._event_task = asyncio.create_task(self._run_event_listener())

    async def _run_event_listener(self):
        """Holds one persistent WS connection subscribed to all job
        events for the addon's lifetime, reconnecting on drop.
        """
        while True:
            try:
                self._event_session = aiohttp.ClientSession()
                self._event_ws = await self._event_session.ws_connect(self.ws_url, timeout=15)
                await self._event_ws.receive_json()  # server info frame
                await self._event_ws.send_json({
                    "command": "subscribe_events", "message_id": "sub", "args": {}
                })
                first = await self._event_ws.receive_json()
                if first.get("event") != "initial_state":
                    _LOGGER.warning("Unexpected subscribe_events response: %s", first)
                _LOGGER.info("Job event listener subscribed")

                while True:
                    msg = await self._event_ws.receive_json()
                    self._handle_event(msg)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.warning("Job event listener disconnected: %s — reconnecting in 5s", err)
            finally:
                try:
                    if self._event_ws is not None:
                        await self._event_ws.close()
                    if self._event_session is not None:
                        await self._event_session.close()
                except Exception:
                    pass
            await asyncio.sleep(5)

    def _handle_event(self, msg: dict):
        event = msg.get("event")
        data = msg.get("data", {})

        if event == "job_output":
            job_id = data.get("job_id")
            job = self._esphome_job_map.get(job_id)
            if job is not None:
                job.output.append(_strip_ansi(data.get("line", "")))
            return

        if event == "job_queued":
            job_info = data.get("job", {})
            job_id = job_info.get("job_id")
            job_type = job_info.get("job_type")
            configuration = job_info.get("configuration")
            if job_type == "upload" and configuration in self._pending_installs:
                job = self._pending_installs.pop(configuration)
                self._esphome_job_map[job_id] = job
                job.flash_output_start_index = len(job.output)
                _LOGGER.info("Discovered chained upload job for %s: %s",
                             job.device_name, job_id)
            return

        if event in ("job_completed", "job_failed"):
            job_info = data.get("job", {})
            job_id = job_info.get("job_id")
            job = self._esphome_job_map.get(job_id)
            if job is None:
                return
            status = job_info.get("status", "failed")
            job.status = status
            job.exit_code = job_info.get("exit_code")
            job.error = job_info.get("error")
            # A completed COMPILE phase of an install that has no
            # upload chained yet, but failed, means no upload is coming.
            if job.job_type == "install" and status != "completed" \
                    and job.configuration in self._pending_installs:
                self._pending_installs.pop(job.configuration, None)
                job.completed_at = time.time()
            # Only mark the overall Job completed_at once we're on its
            # LAST phase: for compile-only jobs immediately; for install
            # jobs, only once the flash phase itself has a result (i.e.
            # flash_output_start_index is set and this event's job_id is
            # not the compile job_id anymore) OR the compile itself failed.
            if job.job_type == "compile":
                job.completed_at = time.time()
            elif job.job_type == "install" and job.flash_output_start_index is not None:
                job.completed_at = time.time()
            _LOGGER.info("Job event %s for %s (job_id=%s): status=%s exit_code=%s",
                         event, job.device_name, job_id, job.status, job.exit_code)
            return

    async def validate_config(self, device_name: str) -> dict:
        """Runs devices/validate and waits for the terminal result.
        Only catches YAML/schema errors, not C++ build errors. Uses its
        own short-lived connection (independent of the event listener).
        """
        configuration = f"{device_name}.yaml"
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(self.ws_url, timeout=15)
        output_lines: list[str] = []
        try:
            await ws.receive_json()
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
        """Starts firmware/compile. Output/status are collected purely
        via the persistent subscribe_events listener from here on.
        Returns OUR OWN job_id.
        """
        configuration = f"{device_name}.yaml"
        esphome_job_id = await self._send_job_command("firmware/compile", configuration)

        our_job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=our_job_id,
            device_name=device_name,
            job_type="compile",
            configuration=configuration,
        )
        self.jobs[our_job_id] = job
        self._esphome_job_map[esphome_job_id] = job
        _LOGGER.info("Compile started for %s: our_job_id=%s esphome_job_id=%s",
                     device_name, our_job_id, esphome_job_id)
        return our_job_id

    async def start_install(self, device_name: str) -> str:
        """Starts firmware/install with port=OTA (WiFi). The chained
        upload job is discovered automatically by the event listener
        via _pending_installs. Returns OUR OWN job_id.
        """
        configuration = f"{device_name}.yaml"
        esphome_job_id = await self._send_job_command(
            "firmware/install", configuration, extra_args={"port": "OTA"}
        )

        our_job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=our_job_id,
            device_name=device_name,
            job_type="install",
            configuration=configuration,
        )
        self.jobs[our_job_id] = job
        self._esphome_job_map[esphome_job_id] = job
        self._pending_installs[configuration] = job
        _LOGGER.info("Install (OTA) started for %s: our_job_id=%s esphome_compile_job_id=%s",
                     device_name, our_job_id, esphome_job_id)
        return our_job_id

    async def _send_job_command(self, command: str, configuration: str,
                                  extra_args: Optional[dict] = None) -> str:
        """Sends a one-shot firmware/compile or firmware/install command
        on a short-lived connection and returns the resulting job_id.
        The persistent event listener (already running) picks up all
        further output/status for this job_id.
        """
        args = {"configuration": configuration}
        if extra_args:
            args.update(extra_args)
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(self.ws_url, timeout=15)
        try:
            await ws.receive_json()
            await ws.send_json({"command": command, "message_id": "1", "args": args})
            msg = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if "error_code" in msg:
                raise RuntimeError(f"{msg.get('error_code')}: {msg.get('details')}")
            return msg["result"]["job_id"]
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
            "flash_phase_started": job.flash_output_start_index is not None,
        }

    def get_full_log(self, job_id: str) -> Optional[str]:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return "".join(job.output)

    def get_flash_log(self, job_id: str) -> Optional[dict]:
        """Only the flash/upload phase's own output, skipping the
        compile portion (which can already be inspected separately via
        the standalone compile endpoint).
        """
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.job_type != "install":
            return {"log": "", "note": "Dieser Job hat keine Flash-Phase (job_type != 'install')."}
        if job.flash_output_start_index is None:
            return {"log": "", "note": "Flash-Phase hat noch nicht begonnen (Compile läuft noch oder ist fehlgeschlagen)."}
        return {"log": "".join(job.output[job.flash_output_start_index:]), "note": None}

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
