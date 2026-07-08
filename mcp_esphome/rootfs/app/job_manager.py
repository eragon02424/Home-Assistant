"""Job Manager - talks to the ESPHome Device Builder's WebSocket API.

As of ESPHome 2026.6.0 the legacy REST dashboard API (POST /api/compile,
POST /api/upload) no longer exists. The new "ESPHome Device Builder"
backend is WS-first: everything goes through ws://<host>:6052/ws with a
command/response protocol.

  - Connect: ws://localhost:6052/ws
  - First frame from server: {"server_version", "esphome_version", "port",
    "ha_addon", "ha_ingress", "requires_auth"}. requires_auth is False in
    this HA-addon setup, so commands can be sent immediately.
  - devices/validate {configuration}: streams {"event":"output","data":line}
    frames, terminated by {"event":"result","data":{"success","code"}}.
    Only catches YAML/schema errors, NOT C++ build errors.
  - firmware/compile {configuration}: returns immediately with
    {"result": <FirmwareJob>}. Only one compile runs at a time; others queue.
  - firmware/install {configuration, port}: same shape as compile, but
    once the compile phase succeeds ESPHome AUTOMATICALLY chains a
    SEPARATE upload job (its own new job_id, linked via depends_on back
    to the compile job_id) that performs the actual flash. Confirmed by
    testing: this chained job_id is never returned to the caller
    directly by firmware/install, and there is no documented WS command
    to list jobs for a device/configuration. The only way found to
    discover it is to watch the ESPHome addon's OWN service log for the
    line "Starting job <id>: upload <configuration>" that appears the
    moment the chained job starts. This requires hassio_api/manager
    permissions (see config.yaml) to read that addon's Supervisor logs
    from inside our own container.
  - firmware/get_job {job_id}: returns the current FirmwareJob snapshot.
    CONFIRMED: once a job reaches a terminal state, ESPHome clears
    "output" back to [] and "error" stays null, even for real errors.
    Polling get_job AFTER completion cannot recover the error text.
  - firmware/follow_job {job_id}: streams the SAME output frames live,
    terminated by a "result" event. This is the ONLY way to capture
    build/upload output/errors -- must be attached while the job runs.

Because of this, jobs here are tracked with our OWN in-memory Job
objects that accumulate output via firmware/follow_job as it streams,
independent of what ESPHome's own job objects still hold once finished.
For install(), TWO phases (compile, then upload) are followed in
sequence and their output is concatenated into the same Job.
"""
import asyncio
import logging
import os
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
        self._esphome_addon_slug: Optional[str] = None

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
        firmware/follow_job in a background task to capture output live.
        Returns OUR OWN job_id (not ESPHome's).
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
        job.follow_task = asyncio.create_task(self._follow_one_phase(job, job.esphome_job_id, "compile"))
        _LOGGER.info("Compile started for %s: our_job_id=%s esphome_job_id=%s",
                     device_name, our_job_id, esphome_job["job_id"])
        return our_job_id

    async def start_install(self, device_name: str) -> str:
        """Starts firmware/install with port=OTA (WiFi). ESPHome chains
        a compile phase followed automatically by a separate upload
        phase (own job_id). Both are followed live and their output is
        concatenated into the same Job.
        """
        configuration = f"{device_name}.yaml"
        session, ws = await self._connect()
        try:
            await ws.send_json({
                "command": "firmware/install",
                "message_id": "1",
                "args": {"configuration": configuration, "port": "OTA"},
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
            job_type="install",
            status=esphome_job["status"],
        )
        self.jobs[our_job_id] = job
        job.follow_task = asyncio.create_task(self._run_install(job, configuration))
        _LOGGER.info("Install (OTA) started for %s: our_job_id=%s esphome_compile_job_id=%s",
                     device_name, our_job_id, esphome_job["job_id"])
        return our_job_id

    async def _run_install(self, job: Job, configuration: str):
        compile_ok = await self._follow_one_phase(job, job.esphome_job_id, "compile")
        if not compile_ok:
            return  # compile itself failed; job.status/exit_code/error already set

        job.output.append("\n--- compile OK — suche verketteten Upload-Job ---\n")
        upload_job_id = await self._find_chained_job_id(configuration, timeout=30)
        if upload_job_id is None:
            job.output.append(
                "Kein Upload-Job innerhalb von 30s gefunden — "
                "Installation eventuell nicht fortgesetzt oder Erkennung fehlgeschlagen.\n"
            )
            job.status = "unknown"
            job.completed_at = time.time()
            _LOGGER.warning("Could not discover chained upload job for %s", job.device_name)
            return

        job.output.append(f"Upload-Job gefunden: {upload_job_id}\n")
        await self._follow_one_phase(job, upload_job_id, "upload")

    async def _follow_one_phase(self, job: Job, esphome_job_id: str, phase_label: str) -> bool:
        """Follows one job phase via firmware/follow_job, appends its
        output to job.output, and updates job.status/exit_code/error
        from its terminal result. Returns True if it completed with
        exit_code 0.
        """
        session, ws = await self._connect()
        try:
            await ws.send_json({
                "command": "firmware/follow_job",
                "message_id": "1",
                "args": {"job_id": esphome_job_id},
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
                    _LOGGER.info("%s phase finished for %s: status=%s exit_code=%s",
                                 phase_label, job.device_name, job.status, job.exit_code)
                    return job.exit_code == 0
                elif "error_code" in msg:
                    job.status = "failed"
                    job.error = f"{msg.get('error_code')}: {msg.get('details')}"
                    job.completed_at = time.time()
                    return False
        except Exception as err:
            job.status = "failed"
            job.error = f"follow_job connection error ({phase_label}): {err}"
            job.completed_at = time.time()
            _LOGGER.error("follow_job failed for %s (%s): %s", job.device_name, phase_label, err)
            return False
        finally:
            await ws.close()
            await session.close()

    async def _find_esphome_addon_slug(self) -> Optional[str]:
        """Finds the ESPHome Device Builder addon's slug (NOT our own
        mcp_esphome addon) via the Supervisor API. Cached after first
        successful lookup. Requires hassio_api: true + hassio_role:
        manager in config.yaml.
        """
        if self._esphome_addon_slug:
            return self._esphome_addon_slug
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            _LOGGER.warning("No SUPERVISOR_TOKEN — cannot discover ESPHome addon slug "
                            "(needs hassio_api permission)")
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://supervisor/addons",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.warning("Supervisor /addons returned HTTP %s", resp.status)
                        return None
                    data = await resp.json()
            for addon in data.get("data", {}).get("addons", []):
                slug = addon.get("slug", "")
                if slug.endswith("_esphome") and "mcp" not in slug.lower():
                    self._esphome_addon_slug = slug
                    _LOGGER.info("Discovered ESPHome addon slug: %s", slug)
                    return slug
        except Exception as err:
            _LOGGER.warning("Could not discover ESPHome addon slug: %s", err)
        return None

    async def _find_chained_job_id(self, configuration: str, timeout: float = 30) -> Optional[str]:
        """Polls the ESPHome addon's own Supervisor service log for a
        'Starting job <id>: upload <configuration>' line. This is the
        only way found to discover the auto-chained upload job_id,
        since the WS API has no list-jobs-for-device command.
        """
        slug = await self._find_esphome_addon_slug()
        if slug is None:
            return None
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        pattern = re.compile(r"Starting job (\w+): upload " + re.escape(configuration))
        deadline = time.time() + timeout
        async with aiohttp.ClientSession() as session:
            while time.time() < deadline:
                try:
                    async with session.get(
                        f"http://supervisor/addons/{slug}/logs",
                        headers={"Authorization": f"Bearer {token}"},
                        params={"lines": 50},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            matches = pattern.findall(text)
                            if matches:
                                return matches[-1]
                except Exception as err:
                    _LOGGER.debug("Chained job discovery poll error: %s", err)
                await asyncio.sleep(1)
        return None

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
