"""
MCP Claude Code für Home Assistant v0.1.0

Startet headless Claude-Code-Jobs (claude -p) auf der dauerhaft laufenden
Linux-VM im eigenen Netz per SSH und erlaubt asynchrones Abfragen von
Status und Log (Job-Queue-Muster, wie mcp_visualstudioconector).

Unterschied zum Visual-Studio-Connector: Zielrechner ist LINUX (bash statt
PowerShell), es gibt keine IDE-Anbindung, und ein Job wird als normaler
Hintergrundprozess gestartet (setsid + nohup) statt über eine geplante
Windows-Aufgabe. Dieses Add-on läuft getrennt vom Windows-Add-on; die fünf
Werkzeuge sind bewusst als eigene Kopie gepflegt.

Ablauf pro Job:
  1. start_task() legt auf der VM unter ~/mcp_claude_code_jobs/<job_id>/ die
     Dateien prompt.txt, run.sh und status.txt an (per SFTP, ohne
     verschachtelte Anführungszeichen) und startet run.sh als eigene
     Sitzung, die das Ende der SSH-Verbindung übersteht.
  2. run.sh schreibt pid, output.jsonl (stream-json) und am Ende status.txt
     (DONE:<exit_code>).
  3. get_job_status() liest status.txt und prüft, ob der Prozess noch lebt.
     Ein Job, dessen Prozess ohne Abschluss verschwunden ist (z. B. VM-
     Neustart), wird als LOST gemeldet und bleibt nicht ewig auf RUNNING.
  4. get_job_log() liest das Ende von output.jsonl.
  5. stop_task() beendet die Prozessgruppe des Jobs.

Anmeldeüberwachung: Im Hintergrund wird regelmäßig `claude auth status` auf
der VM abgefragt. Ist Claude Code nicht angemeldet, erscheint in Home
Assistant eine Benachrichtigung mit dem Hinweis, Claude Code neu anzumelden.
Dieselbe Meldung kommt, wenn ein Job mit einem Anmeldefehler endet.
"""

import asyncio
import functools as _functools
import inspect as _inspect
import json
import logging
import os
import re
import shlex
import time
import uuid
from datetime import datetime
from pathlib import Path

import asyncssh
import httpx
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError as _ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError as _MCPError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

VERSION = "0.1.0"
PORT = 8774

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_claude_code")

for _noisy in ("uvicorn.access", "uvicorn.error", "asyncssh", "httpx", "mcp", "mcp.server", "mcp.server.fastmcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ── Konfiguration aus der Umgebung (gesetzt von run.sh) ─────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")
SSH_HOST = os.environ.get("SSH_HOST", "")
SSH_PORT = int(os.environ.get("SSH_PORT", "22"))
SSH_USER = os.environ.get("SSH_USER", "")
SSH_KEY_PATH = os.environ.get("SSH_KEY_PATH", "")
WORKSPACE_PATH = os.environ.get("WORKSPACE_PATH", "")
CLAUDE_BINARY = os.environ.get("CLAUDE_BINARY", "claude")
LOGIN_CHECK_MINUTES = int(os.environ.get("LOGIN_CHECK_MINUTES", "10") or "10")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

JOBS_REGISTRY_FILE = Path("/data/jobs.json")
NOTIFICATION_ID = "mcp_claude_code_login"
RENOTIFY_SECONDS = 6 * 3600

_SESSION_ID_RE = re.compile(r'"session_id"\s*:\s*"([a-f0-9-]+)"')
_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
LOGIN_ERROR_PATTERN = "OAuth session expired|Failed to authenticate|Please run /login|authentication_error"

log.info("Token auth: %s", "enabled" if TOKEN else "disabled")
log.info("SSH target: %s@%s:%s", SSH_USER, SSH_HOST, SSH_PORT)
log.info("Workspace: %s", WORKSPACE_PATH)

q = shlex.quote

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

# ── SSH-Verbindung (dauerhaft, baut sich bei Abbruch neu auf) ────────────────

_ssh_conn = None
_ssh_lock = asyncio.Lock()


async def _get_connection():
    global _ssh_conn
    async with _ssh_lock:
        if _ssh_conn is not None and not _ssh_conn.is_closed():
            return _ssh_conn
        if not SSH_KEY_PATH or not os.path.exists(SSH_KEY_PATH):
            raise RuntimeError(f"SSH-Schlüssel nicht gefunden: {SSH_KEY_PATH or '(nicht konfiguriert)'}")
        log.info("SSH: Verbindungsaufbau zu %s@%s:%s ...", SSH_USER, SSH_HOST, SSH_PORT)
        _ssh_conn = await asyncssh.connect(
            host=SSH_HOST,
            port=SSH_PORT,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None,
            connect_timeout=15,
        )
        log.info("SSH: Verbunden")
        return _ssh_conn


async def _run(command: str, timeout: int = 30) -> asyncssh.SSHCompletedProcess:
    """Führt einen bash-Befehl auf der Linux-VM aus (bei Verbindungsfehler ein Neuversuch)."""
    global _ssh_conn
    for attempt in (1, 2):
        try:
            conn = await _get_connection()
            return await asyncio.wait_for(conn.run(command, check=False), timeout=timeout)
        except asyncio.TimeoutError:
            raise
        except (asyncssh.Error, ConnectionError, OSError):
            _ssh_conn = None
            if attempt == 2:
                raise


async def _remote_home() -> str:
    r = await _run('printf %s "$HOME"', 15)
    home = (r.stdout or "").strip()
    if not home:
        raise RuntimeError(f"Konnte $HOME auf der VM nicht ermitteln: {r.stderr}")
    return home


# ── Anmeldeüberwachung ───────────────────────────────────────────────────────

_login_state: dict = {"logged_in": None, "checked_at": None, "detail": "noch nicht geprüft"}
_notified_at = 0.0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _ha_notification(create: bool) -> None:
    """Legt die Benachrichtigung in Home Assistant an oder schließt sie (best effort)."""
    if not SUPERVISOR_TOKEN:
        log.warning("SUPERVISOR_TOKEN fehlt - keine Benachrichtigung in Home Assistant möglich.")
        return
    service = "create" if create else "dismiss"
    payload: dict = {"notification_id": NOTIFICATION_ID}
    if create:
        payload["title"] = "Claude Code neu anmelden"
        payload["message"] = (
            f"MCP Claude Code hat erkannt: Claude Code auf der Linux-VM ({SSH_HOST}) ist nicht angemeldet. "
            "Aufträge schlagen fehl, bis die Anmeldung erneuert ist. "
            f"Bitte neu anmelden: per SSH auf die VM verbinden (ssh {SSH_USER}@{SSH_HOST}), "
            "dort `claude` starten und /login ausführen, den Link im Browser öffnen, "
            "anmelden und den angezeigten Code zurück ins Terminal kopieren."
        )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"http://supervisor/core/api/services/persistent_notification/{service}",
                headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
                json=payload,
            )
        if resp.status_code >= 300:
            log.warning("Benachrichtigung (%s) fehlgeschlagen: HTTP %s", service, resp.status_code)
        else:
            log.info("Benachrichtigung in Home Assistant: %s", service)
    except Exception as e:
        log.warning("Benachrichtigung (%s) fehlgeschlagen: %s", service, e)


async def _apply_login_state(logged_in, detail: str) -> None:
    global _notified_at
    _login_state.update(logged_in=logged_in, checked_at=_now(), detail=detail)
    if logged_in is False:
        if time.time() - _notified_at > RENOTIFY_SECONDS:
            await _ha_notification(True)
            _notified_at = time.time()
    elif logged_in is True and _notified_at:
        await _ha_notification(False)
        _notified_at = 0.0


async def _check_login() -> dict:
    """Fragt `claude auth status` auf der VM ab und aktualisiert Zustand und Meldung."""
    try:
        r = await _run(f"{q(CLAUDE_BINARY)} auth status < /dev/null 2>&1", 45)
        out = (r.stdout or "").strip()
        start, end = out.find("{"), out.rfind("}")
        if start < 0 or end < start:
            await _apply_login_state(None, f"Antwort nicht lesbar: {out[:200]}")
        else:
            data = json.loads(out[start:end + 1])
            logged_in = bool(data.get("loggedIn"))
            await _apply_login_state(logged_in, f"authMethod={data.get('authMethod')}")
    except Exception as e:
        await _apply_login_state(None, f"VM nicht erreichbar oder Prüfung fehlgeschlagen: {e}")
    return dict(_login_state)


async def _login_loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            state = await _check_login()
            log.info("Anmeldeprüfung: logged_in=%s (%s)", state["logged_in"], state["detail"])
        except Exception as e:
            log.warning("Anmeldeprüfung abgebrochen: %s", e)
        await asyncio.sleep(max(60, LOGIN_CHECK_MINUTES * 60))


# ── MCP-Server ───────────────────────────────────────────────────────────────

mcp = MCPServer(
    version=VERSION,
    name="MCP Claude Code",
    instructions=(
        "Starts and monitors headless Claude Code jobs (claude -p) on the always-on "
        "LINUX VM in the owner's home network. This is NOT the Windows/Visual Studio "
        "connector. Use start_task to launch a job with a prompt, then poll "
        "get_job_status until it is DONE (or LOST/STOPPED), read the result with "
        "get_job_log, and resume a finished session with resume_session_id. "
        "check_login reports whether Claude Code on the VM is logged in; if it is not, "
        "jobs fail until the owner logs in again (Home Assistant shows a notification)."
    ),
)

# MCP SDK 2.x: Fehlermeldungen von Tools wie in 1.x an das Modell durchreichen.
# Ohne diesen Wrapper sieht das Modell nur "Error executing tool <name>".


def _visible_errors(fn):
    if _inspect.iscoroutinefunction(fn):
        @_functools.wraps(fn)
        async def _wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    else:
        @_functools.wraps(fn)
        def _wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (_ToolError, _MCPError):
                raise
            except Exception as e:
                raise _ToolError(str(e) or type(e).__name__) from e
    return _wrapper


_mcp_tool_orig = mcp.tool


def _mcp_tool(*args, **kwargs):
    decorator = _mcp_tool_orig(*args, **kwargs)
    return lambda fn: decorator(_visible_errors(fn))


mcp.tool = _mcp_tool


# ── Hilfsfunktionen für Jobs ─────────────────────────────────────────────────

async def _get_job(job_id: str) -> dict:
    async with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise ValueError(f"job_id nicht bekannt: {job_id}")
    return job


async def _session_id(remote_dir: str) -> str | None:
    r = await _run(
        f"grep -oE '\"session_id\" *: *\"[a-f0-9-]+\"' {q(remote_dir)}/output.jsonl 2>/dev/null | tail -1",
        20,
    )
    m = _SESSION_ID_RE.search(r.stdout or "")
    return m.group(1) if m else None


async def _has_login_error(remote_dir: str) -> bool:
    r = await _run(f"grep -qE {q(LOGIN_ERROR_PATTERN)} {q(remote_dir)}/output.jsonl 2>/dev/null; echo $?", 20)
    return (r.stdout or "").strip() == "0"


# ── Werkzeuge ────────────────────────────────────────────────────────────────

@mcp.tool()
async def start_task(
    prompt: str,
    resume_session_id: str = "",
    allowed_tools: str = "",
    permission_mode: str = "bypassPermissions",
    model: str = "",
) -> dict:
    """
    Start a headless Claude Code job on the Linux VM.

    Args:
        prompt: The task/instruction for Claude Code (sent via stdin).
        resume_session_id: If set, resumes a previous session instead of
            starting a new one (use the session_id returned by get_job_status
            of a finished job).
        allowed_tools: Optional comma-separated tool allowlist passed to
            --allowedTools (e.g. "Read,Bash(curl:*)"). Empty = default tools.
        permission_mode: --permission-mode value (default "bypassPermissions").
            Headless jobs cannot answer interactive questions, so any
            permission prompt would leave the job hanging; narrow the
            tools with allowed_tools instead if more control is wanted.
        model: Optional --model value (e.g. "claude-haiku-4-5-20251001").
            Empty = default model of the logged-in account.

    Returns:
        dict with job_id to use for get_job_status/get_job_log/stop_task.
    """
    if not WORKSPACE_PATH:
        raise ValueError("WORKSPACE_PATH ist nicht konfiguriert (Add-on-Optionen prüfen).")
    if not prompt.strip():
        raise ValueError("prompt ist leer")
    if permission_mode not in _PERMISSION_MODES:
        raise ValueError(f"permission_mode muss einer von {sorted(_PERMISSION_MODES)} sein")
    if resume_session_id and not re.fullmatch(r"[a-f0-9-]{8,64}", resume_session_id):
        raise ValueError("resume_session_id hat kein gültiges Format")
    if model and not re.fullmatch(r"[A-Za-z0-9._\-\[\]]+", model):
        raise ValueError("model hat kein gültiges Format")

    job_id = uuid.uuid4().hex[:12]
    log.info("start_task job=%s resume=%s prompt=%s", job_id, resume_session_id or "-", prompt[:120].replace("\n", " "))

    home = await _remote_home()
    job_dir = f"{home}/mcp_claude_code_jobs/{job_id}"
    r = await _run(f"mkdir -p {q(job_dir)} {q(WORKSPACE_PATH)}", 20)
    if r.exit_status != 0:
        raise RuntimeError(f"Konnte Job- oder Arbeitsverzeichnis nicht anlegen: {r.stderr}")

    args = ["--output-format", "stream-json", "--verbose", "--permission-mode", permission_mode]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]
    if model:
        args += ["--model", model]
    args_sh = " ".join(q(a) for a in args)

    script = (
        "#!/bin/bash\n"
        f"JOB_DIR={q(job_dir)}\n"
        f"cd {q(WORKSPACE_PATH)} || {{ echo 'FEHLER: Arbeitsverzeichnis nicht erreichbar' > \"$JOB_DIR/output.jsonl\"; "
        "echo 'DONE:1' > \"$JOB_DIR/status.txt\"; exit 1; }\n"
        "echo $$ > \"$JOB_DIR/pid\"\n"
        f"{q(CLAUDE_BINARY)} -p {args_sh} < \"$JOB_DIR/prompt.txt\" > \"$JOB_DIR/output.jsonl\" 2>&1\n"
        "echo \"DONE:$?\" > \"$JOB_DIR/status.txt\"\n"
    )

    try:
        conn = await _get_connection()
        async with conn.start_sftp_client() as sftp:
            for name, content in (("prompt.txt", prompt), ("run.sh", script), ("status.txt", "RUNNING\n")):
                async with sftp.open(f"{job_dir}/{name}", "w", encoding="utf-8") as f:
                    await f.write(content)
    except Exception as e:
        raise RuntimeError(f"SFTP-Fehler beim Anlegen des Jobs: {e}") from e

    # setsid -f startet run.sh als eigene Sitzung; sie überlebt das Ende der SSH-Verbindung.
    r = await _run(f"cd {q(job_dir)} && setsid -f nohup bash run.sh </dev/null >/dev/null 2>&1", 30)
    if r.exit_status != 0:
        raise RuntimeError(f"Konnte Job nicht starten: {r.stderr}")

    async with _jobs_lock:
        _jobs[job_id] = {
            "remote_dir": job_dir,
            "workspace": WORKSPACE_PATH,
            "prompt_preview": prompt[:200],
            "resume_of": resume_session_id or None,
            "started": time.time(),
            "started_at": _now(),
        }
        _save_jobs()

    return {"success": True, "job_id": job_id}


@mcp.tool()
async def get_job_status(job_id: str) -> dict:
    """
    Check the state of a job started with start_task.

    status is RUNNING, DONE (with exit_code, and session_id for resume),
    STOPPED (stopped by stop_task) or LOST (the process vanished without a
    result, e.g. after a VM restart). If a finished job failed because Claude
    Code is not logged in, login_error is true.
    """
    job = await _get_job(job_id)
    d = q(job["remote_dir"])
    r = await _run(
        f"cat {d}/status.txt 2>/dev/null; echo; echo ---PID---; "
        f"if [ -f {d}/pid ]; then if kill -0 \"$(cat {d}/pid)\" 2>/dev/null; then echo alive; else echo dead; fi; "
        f"else echo nopid; fi",
        20,
    )
    status_part, _, pid_part = (r.stdout or "").partition("---PID---")
    status_raw = status_part.strip()
    pid_state = pid_part.strip()

    result: dict = {"success": True, "job_id": job_id}
    if status_raw.startswith("DONE:"):
        try:
            exit_code = int(status_raw.split(":", 1)[1].strip())
        except ValueError:
            exit_code = -1
        result.update(status="DONE", exit_code=exit_code)
        if exit_code != 0 and await _has_login_error(job["remote_dir"]):
            result["login_error"] = True
            result["hint"] = "Claude Code auf der VM ist nicht (mehr) angemeldet - neu anmelden (siehe check_login)."
            await _apply_login_state(False, "Anmeldefehler in einem Job erkannt")
    elif status_raw.startswith("STOPPED"):
        result["status"] = "STOPPED"
    else:
        age = time.time() - float(job.get("started", 0) or 0)
        if pid_state == "alive" or (pid_state == "nopid" and age < 30):
            result["status"] = "RUNNING"
            result["running_seconds"] = int(age)
        else:
            result["status"] = "LOST"
            result["hint"] = (
                "Der Prozess auf der VM läuft nicht mehr und hat keinen Abschluss gemeldet "
                "(vermutlich VM- oder Prozessabbruch). Log mit get_job_log prüfen, ggf. mit "
                "resume_session_id neu starten."
            )
    if result["status"] in ("DONE", "LOST"):
        sid = await _session_id(job["remote_dir"])
        if sid:
            result["session_id"] = sid
    return result


@mcp.tool()
async def get_job_log(job_id: str, tail_lines: int = 200) -> dict:
    """
    Fetch the (buffered) stream-json output of a job.

    Args:
        job_id: Job returned by start_task.
        tail_lines: Only return the last N lines (default 200; 0 = whole log,
            capped at 200000 bytes).
    """
    job = await _get_job(job_id)
    d = q(job["remote_dir"])
    if tail_lines and tail_lines > 0:
        cmd = f"tail -n {int(tail_lines)} {d}/output.jsonl 2>/dev/null | tail -c 200000"
    else:
        cmd = f"tail -c 200000 {d}/output.jsonl 2>/dev/null"
    r = await _run(cmd, 30)
    return {"success": True, "job_id": job_id, "log": r.stdout}


@mcp.tool()
async def stop_task(job_id: str) -> dict:
    """Best-effort stop of a running job (terminates the job's process group)."""
    job = await _get_job(job_id)
    d = q(job["remote_dir"])
    r = await _run(
        f"if [ -f {d}/pid ]; then kill -TERM -- -\"$(cat {d}/pid)\" 2>&1 && echo stopped || echo 'kill fehlgeschlagen'; "
        f"else echo 'keine pid-Datei'; fi; "
        f"if ! grep -q '^DONE' {d}/status.txt 2>/dev/null; then echo STOPPED > {d}/status.txt; fi",
        20,
    )
    return {"success": r.exit_status == 0, "output": (r.stdout or "").strip(), "error": r.stderr}


@mcp.tool()
async def list_jobs() -> dict:
    """List all jobs known to this add-on (the list survives add-on restarts)."""
    async with _jobs_lock:
        return {"success": True, "jobs": _jobs}


@mcp.tool()
async def check_login() -> dict:
    """
    Check now whether Claude Code on the Linux VM is logged in.

    logged_in is true, false, or null (null = could not be determined, e.g.
    the VM is unreachable; see detail). If false, jobs fail until the owner
    logs in again; Home Assistant shows a notification about it.
    """
    state = await _check_login()
    return {"success": True, **state}


# ── Token-Prüfung ────────────────────────────────────────────────────────────

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


# ── App ──────────────────────────────────────────────────────────────────────

# DNS-Rebinding-Schutz aus, damit Anfragen über LAN-IP/Proxy akzeptiert werden
app = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)
app.add_middleware(TokenAuthMiddleware)


async def _main() -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    login_task = asyncio.create_task(_login_loop())
    try:
        await server.serve()
    finally:
        login_task.cancel()


if __name__ == "__main__":
    asyncio.run(_main())
