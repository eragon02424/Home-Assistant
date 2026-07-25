"""
MCP Visual Studio Connector for Home Assistant v0.1.0

Startet headless Claude-Code-Jobs (claude -p ... --ide) auf einer entfernten
Windows-Maschine (VM mit Visual Studio + firish/claude_code_vs Erweiterung)
per SSH und erlaubt asynchrones Abfragen von Status/Log (Job-Queue-Pattern,
analog zum ESPHome-Vermittler-Service).

Wichtig: Der Zielrechner ist WINDOWS, nicht Linux - alle Remote-Befehle sind
PowerShell, nicht bash. Das unterscheidet dieses Addon von mcp_shell.

Ablauf pro Job:
  1. start_task() legt unter %TEMP%\\mcp_vs_jobs\\<job_id>\\ auf dem Windows-
     Rechner ein Job-Verzeichnis an (prompt.txt, run.ps1) und startet dort per
     Start-Process (detached, WindowStyle Hidden) ein PowerShell-Skript, das:
       - ins Workspace-Verzeichnis wechselt (damit .mcp.json / vs-debug /
         vs-semantic geladen werden)
       - den Prompt per stdin an `claude -p --output-format stream-json
         --verbose --ide` übergibt
       - die komplette Ausgabe nach output.jsonl umleitet
       - nach Abschluss "DONE:<exitcode>" in status.txt schreibt
  2. get_job_status() liest status.txt (oder "RUNNING" falls noch nicht da)
  3. get_job_log() liest output.jsonl (optional nur die letzten N Zeilen)
  4. stop_task() beendet den Prozessbaum per taskkill (best effort)

Job-Registry liegt zusätzlich lokal unter /data/jobs.json (einfache
Wiederherstellung nach Addon-Neustart; kein Anspruch auf Vollständigkeit,
falls der Windows-Host zwischenzeitlich neu gestartet wurde).
"""

import asyncio
import json
import logging
import os
import re
import uuid
from pathlib import Path

import asyncssh
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

# ── Logging setup ────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_vs_connector")

for _noisy in ("uvicorn.access", "uvicorn.error", "asyncssh", "mcp", "mcp.server", "mcp.server.fastmcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Config from environment (gesetzt von run.sh aus HA Add-on Optionen) ─────

TOKEN = os.environ.get("MCP_TOKEN", "")
SSH_HOST = os.environ.get("SSH_HOST", "")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "/data/ssh_key/mcp_vs_key")
SSH_PASSWORD = os.environ.get("SSH_PASSWORD", "")  # optionaler Fallback
WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", "")
CLAUDE_BINARY = os.environ.get("CLAUDE_BINARY", "claude")

JOBS_REGISTRY_FILE = Path("/data/jobs.json")
REMOTE_JOBS_BASE = "$env:TEMP\\mcp_vs_jobs"  # wird pro Job um \<job_id> ergänzt

log.info("Token auth: %s", "enabled" if TOKEN else "disabled")
log.info("SSH target: %s@%s:%s", SSH_USER, SSH_HOST, SSH_PORT)
log.info("Workspace: %s", WORKSPACE_PATH)

# ── Job-Registry (lokal, einfache Persistenz) ────────────────────────────────

_jobs: dict[str, dict] = {}
_jobs_lock = asyncio.Lock()


def _load_jobs():
    global _jobs
    if JOBS_REGISTRY_FILE.exists():
        try:
            _jobs = json.loads(JOBS_REGISTRY_FILE.read_text())
        except Exception:
            _jobs = {}


def _save_jobs():
    try:
        JOBS_REGISTRY_FILE.write_text(json.dumps(_jobs, indent=2))
    except Exception as e:
        log.warning("Konnte jobs.json nicht schreiben: %s", e)


_load_jobs()

# ── SSH connection (persistent, auto-reconnect) ──────────────────────────────

_ssh_conn = None
_ssh_lock = asyncio.Lock()


async def _get_connection():
    global _ssh_conn
    async with _ssh_lock:
        if _ssh_conn is not None and not _ssh_conn.is_closed():
            return _ssh_conn
        log.warning("SSH: Verbindungsaufbau zu %s@%s:%s ...", SSH_USER, SSH_HOST, SSH_PORT)
        connect_kwargs = dict(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            known_hosts=None,
        )
        if os.path.exists(SSH_KEY_PATH):
            connect_kwargs["client_keys"] = [SSH_KEY_PATH]
        elif SSH_PASSWORD:
            connect_kwargs["password"] = SSH_PASSWORD
        else:
            raise RuntimeError(
                "Weder SSH-Key noch SSH_PASSWORD verfügbar - Verbindung nicht möglich."
            )
        _ssh_conn = await asyncssh.connect(**connect_kwargs)
        log.info("SSH: Verbunden")
        return _ssh_conn


async def _run_ps(command: str, timeout: int = 30) -> asyncssh.SSHCompletedProcess:
    """Führt einen PowerShell-Befehl auf dem Windows-Host aus."""
    conn = await _get_connection()
    full_cmd = f'powershell -NoProfile -NonInteractive -Command "{command}"'
    return await asyncio.wait_for(conn.run(full_cmd, check=False), timeout=timeout)


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="MCP Visual Studio Connector",
    instructions=(
        "Starts and monitors headless Claude Code jobs on a remote Windows "
        "machine that has Visual Studio + the claude_code_vs extension "
        "running. Jobs run with --ide so vs-debug/vs-semantic (debugger, "
        "test explorer, code navigation) are available in addition to the "
        "normal Claude Code toolset. Use start_task to launch a job, then "
        "poll get_job_status/get_job_log until it finishes."
    ),
)

mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Helper ────────────────────────────────────────────────────────────────────

_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([a-f0-9-]+)"')


def _extract_last_session_id(log_text: str) -> str | None:
    matches = _SESSION_ID_RE.findall(log_text)
    return matches[-1] if matches else None


def _job_dir_ps(job_id: str) -> str:
    return f"{REMOTE_JOBS_BASE}\\{job_id}"


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
async def start_task(
    prompt: str,
    resume_session_id: str = "",
    allowed_tools: str = "",
    permission_mode: str = "acceptEdits",
) -> dict:
    """
    Start a headless Claude Code job on the remote Windows machine.

    Args:
        prompt: The task/instruction for Claude Code (sent via stdin).
        resume_session_id: If set, resumes a previous session instead of
            starting a new one (use the session_id returned by get_job_status
            of a finished job).
        allowed_tools: Optional comma-separated tool allowlist passed to
            --allowedTools (e.g. "mcp__vs-debug__*,Read,Edit"). Empty = all
            tools available in the project are permitted.
        permission_mode: --permission-mode value (default "acceptEdits").
            Use "bypassPermissions" only if you understand the risk.

    Returns:
        dict with job_id to use for get_job_status/get_job_log/stop_task.
    """
    if not WORKSPACE_PATH:
        return {"success": False, "error": "WORKSPACE_PATH ist nicht konfiguriert (Add-on-Optionen prüfen)."}

    job_id = uuid.uuid4().hex[:12]
    job_dir = _job_dir_ps(job_id)

    log.info("start_task job=%s resume=%s prompt=%s", job_id, resume_session_id or "-", prompt[:120])

    extra_args = ["--output-format", "stream-json", "--verbose", "--ide", "--permission-mode", permission_mode]
    if resume_session_id:
        extra_args += ["--resume", resume_session_id]
    if allowed_tools:
        extra_args += ["--allowedTools", allowed_tools]
    args_ps = " ".join(f"'{a}'" for a in extra_args)

    # 1) Job-Verzeichnis anlegen
    mkdir_cmd = f"New-Item -ItemType Directory -Force -Path '{job_dir}' | Out-Null"
    r = await _run_ps(mkdir_cmd)
    if r.exit_status != 0:
        return {"success": False, "error": f"Konnte Job-Verzeichnis nicht anlegen: {r.stderr}"}

    # 2) prompt.txt und run.ps1 per SFTP hochladen
    try:
        conn = await _get_connection()
        async with conn.start_sftp_client() as sftp:
            # PowerShell nutzt $env:TEMP als Basis (REMOTE_JOBS_BASE) - für
            # SFTP brauchen wir den tatsächlich expandierten Pfad:
            r3 = await _run_ps("Write-Output $env:TEMP", timeout=15)
            temp_dir = r3.stdout.strip()
            real_job_dir = f"{temp_dir}\\mcp_vs_jobs\\{job_id}"

            async with sftp.open(f"{real_job_dir}\\prompt.txt", "w", encoding="utf-8") as f:
                await f.write(prompt)

            run_ps1 = (
                f"Set-Location -LiteralPath '{WORKSPACE_PATH}'\n"
                f"Get-Content -Raw -LiteralPath '{real_job_dir}\\prompt.txt' "
                f"| & '{CLAUDE_BINARY}' -p {args_ps} *> '{real_job_dir}\\output.jsonl'\n"
                f"\"DONE:$LASTEXITCODE\" | Out-File -Encoding utf8 -LiteralPath '{real_job_dir}\\status.txt'\n"
            )
            async with sftp.open(f"{real_job_dir}\\run.ps1", "w", encoding="utf-8") as f:
                await f.write(run_ps1)
    except Exception as e:
        log.error("SFTP-Fehler beim Anlegen des Jobs: %s", e)
        return {"success": False, "error": f"SFTP error: {e}"}

    # 3) status.txt initial auf RUNNING, dann Skript detached starten
    start_cmd = (
        f"'RUNNING' | Out-File -Encoding utf8 -LiteralPath '{real_job_dir}\\status.txt'; "
        f"$p = Start-Process -FilePath 'powershell.exe' -ArgumentList "
        f"'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','{real_job_dir}\\run.ps1' "
        f"-WindowStyle Hidden -PassThru; "
        f"$p.Id | Out-File -Encoding ascii -LiteralPath '{real_job_dir}\\launcher_pid.txt'"
    )
    r4 = await _run_ps(start_cmd, timeout=30)
    if r4.exit_status != 0:
        return {"success": False, "error": f"Konnte Job nicht starten: {r4.stderr}", "job_id": job_id}

    async with _jobs_lock:
        _jobs[job_id] = {
            "remote_dir": real_job_dir,
            "workspace": WORKSPACE_PATH,
            "prompt_preview": prompt[:200],
            "resume_of": resume_session_id or None,
        }
        _save_jobs()

    return {"success": True, "job_id": job_id}


@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """
    Check whether a job started with start_task is still running or finished.

    Returns status "RUNNING", "DONE" (with exit_code), or "UNKNOWN" if the
    job_id is not known. When DONE, also returns the last session_id found
    in the log (use it as resume_session_id in a follow-up start_task call).
    """
    async with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"success": False, "status": "UNKNOWN", "error": "job_id nicht bekannt"}

    remote_dir = job["remote_dir"]
    r = await _run_ps(
        f"if (Test-Path '{remote_dir}\\status.txt') "
        f"{{ Get-Content -Raw -LiteralPath '{remote_dir}\\status.txt' }} "
        f"else {{ Write-Output 'RUNNING' }}",
        timeout=15,
    )
    status_raw = r.stdout.strip()

    result = {"success": True, "job_id": job_id}
    if status_raw.startswith("DONE:"):
        result["status"] = "DONE"
        result["exit_code"] = int(status_raw.split(":", 1)[1] or -1)
        # Session-ID fürs Resume aus dem Log ziehen
        log_r = await _run_ps(f"Get-Content -LiteralPath '{remote_dir}\\output.jsonl'", timeout=20)
        session_id = _extract_last_session_id(log_r.stdout or "")
        if session_id:
            result["session_id"] = session_id
    else:
        result["status"] = "RUNNING"
    return result


@mcp.tool()
async def get_job_log(job_id: str, tail_lines: int = 200) -> dict:
    """
    Fetch the (buffered) stream-json output of a job.

    Args:
        job_id: Job returned by start_task.
        tail_lines: Only return the last N lines (default 200; use a large
            number or 0 to fetch the full log).
    """
    async with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"success": False, "error": "job_id nicht bekannt"}

    remote_dir = job["remote_dir"]
    if tail_lines and tail_lines > 0:
        cmd = f"if (Test-Path '{remote_dir}\\output.jsonl') {{ Get-Content -Tail {tail_lines} -LiteralPath '{remote_dir}\\output.jsonl' }}"
    else:
        cmd = f"if (Test-Path '{remote_dir}\\output.jsonl') {{ Get-Content -LiteralPath '{remote_dir}\\output.jsonl' }}"
    r = await _run_ps(cmd, timeout=30)
    return {"success": True, "job_id": job_id, "log": r.stdout}


@mcp.tool()
async def stop_task(job_id: str) -> dict:
    """Best-effort stop of a running job (kills the process tree via taskkill)."""
    async with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"success": False, "error": "job_id nicht bekannt"}

    remote_dir = job["remote_dir"]
    r = await _run_ps(
        f"if (Test-Path '{remote_dir}\\launcher_pid.txt') "
        f"{{ $procId = Get-Content -LiteralPath '{remote_dir}\\launcher_pid.txt'; "
        f"taskkill /PID $procId /T /F }} else {{ Write-Output 'no pid file' }}",
        timeout=15,
    )
    return {"success": r.exit_status == 0, "output": r.stdout, "error": r.stderr}


@mcp.tool()
async def list_jobs() -> dict:
    """List all jobs known to this add-on instance since its last restart."""
    async with _jobs_lock:
        return {"success": True, "jobs": _jobs}


# ── Token auth middleware ─────────────────────────────────────────────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not TOKEN:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        token_param = request.query_params.get("token", "")
        if auth_header == f"Bearer {TOKEN}" or token_param == TOKEN:
            return await call_next(request)
        log.warning("Unauthorized request from %s", request.client)
        return Response("Unauthorized", status_code=401)


# ── App assembly ──────────────────────────────────────────────────────────────

app = mcp.streamable_http_app()
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8768, log_level="warning")
