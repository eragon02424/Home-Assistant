"""Job Manager - handles compile/install jobs via the ESPHome dashboard.

Simple async job queue: start a job, poll status, get error summary or full log.
"""
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

_LOGGER = logging.getLogger("mcp_esphome.job_manager")


@dataclass
class Job:
    job_id: str
    device_name: str
    job_type: str  # "compile" or "install"
    status: str = "running"  # running, success, failed
    log_lines: list = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


class JobManager:
    def __init__(self, esphome_dashboard_url: str):
        self.esphome_dashboard_url = esphome_dashboard_url.rstrip("/")
        self.jobs: dict[str, Job] = {}

    async def start_compile(self, device_name: str) -> str:
        return await self._start_job(device_name, "compile")

    async def start_install(self, device_name: str) -> str:
        return await self._start_job(device_name, "install")

    async def _start_job(self, device_name: str, job_type: str) -> str:
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, device_name=device_name, job_type=job_type)
        self.jobs[job_id] = job
        asyncio.create_task(self._run_job(job))
        return job_id

    async def _run_job(self, job: Job):
        endpoint = "compile" if job.job_type == "compile" else "upload"
        url = f"{self.esphome_dashboard_url}/api/{endpoint}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={"configuration": f"{job.device_name}.yaml"},
                    timeout=aiohttp.ClientTimeout(total=600),
                ) as resp:
                    text = await resp.text()
                    job.log_lines = text.splitlines()
                    job.status = "success" if resp.status == 200 else "failed"
        except asyncio.TimeoutError:
            job.status = "failed"
            job.log_lines.append("ERROR: Job timed out after 600s")
        except Exception as err:
            job.status = "failed"
            job.log_lines.append(f"ERROR: {err}")
        finally:
            job.finished_at = time.time()
            _LOGGER.info("Job %s (%s/%s) finished: %s", job.job_id, job.device_name, job.job_type, job.status)

    def get_status(self, job_id: str) -> Optional[dict]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        return {
            "job_id": job.job_id,
            "device_name": job.device_name,
            "job_type": job.job_type,
            "status": job.status,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    def get_error_summary(self, job_id: str, context_lines: int = 30) -> Optional[str]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        error_idx = None
        for i, line in enumerate(job.log_lines):
            if "error" in line.lower():
                error_idx = i
                break
        if error_idx is None:
            return "\n".join(job.log_lines[-context_lines:])
        return "\n".join(job.log_lines[error_idx:error_idx + context_lines])

    def get_full_log(self, job_id: str) -> Optional[str]:
        job = self.jobs.get(job_id)
        if not job:
            return None
        return "\n".join(job.log_lines)
