"""
MCP Second Brain Server for Home Assistant v1.0.0

Exposes exactly ONE folder (the "Second Brain", default /share/second_brain)
to MCP clients. Every path is relative to that root; nothing outside it is
reachable. Deleted notes are moved into <root>/.trash instead of being removed.
"""

import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
import uvicorn

VERSION = "1.0.0"

# ── Config from environment ──────────────────────────────────────────────────

TOKEN = os.environ.get("MCP_TOKEN", "")
ROOT = Path(os.environ.get("SB_ROOT", "/share/second_brain")).resolve()
PORT = int(os.environ.get("SB_PORT", "8773"))
READ_ONLY = os.environ.get("SB_READ_ONLY", "false").lower() == "true"
MAX_FILE_BYTES = int(os.environ.get("SB_MAX_FILE_KB", "512")) * 1024

ALLOWED_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".csv"}
TRASH_DIR = ".trash"
README = "README.md"

ROOT.mkdir(parents=True, exist_ok=True)

print(f"[Second Brain] v{VERSION} root={ROOT} read_only={READ_ONLY} "
      f"token={'enabled' if TOKEN else 'DISABLED'}", flush=True)

# ── FastMCP server ────────────────────────────────────────────────────────────

INSTRUCTIONS = (
    "Second Brain: Jonathans persönlicher, dauerhafter Wissensspeicher (Markdown-Notizen). "
    "Alle Pfade sind RELATIV zum Second-Brain-Stammordner (z. B. '10_Projekte/hundebox.md'). "
    "Zu Beginn einer Aufgabe, die Jonathans Projekte, Geräte, Entscheidungen oder Vorlieben "
    "betrifft: sb_overview aufrufen und die Konventionen aus README.md befolgen, dann mit "
    "sb_search relevante Notizen suchen. "
    "Dauerhaft relevante neue Informationen (Entscheidungen, Konfigurationen, Messwerte, "
    "Erkenntnisse, Projektstände) selbstständig ablegen, ohne dass Jonathan 'speichere das' sagen muss: "
    "bestehende Notiz ergänzen (sb_append / sb_edit) statt Duplikate anzulegen; "
    "unklar zuzuordnendes in 00_Inbox. Keine Passwörter, Tokens oder Zugangsdaten speichern."
)

mcp = FastMCP(name="Second Brain", instructions=INSTRUCTIONS)
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# ── Path helpers ──────────────────────────────────────────────────────────────

def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix() or "."


def _resolve(path: str) -> Path:
    """Map a user supplied relative path onto ROOT; refuse anything outside."""
    path = (path or "").strip().replace("\\", "/")
    # tolerate absolute paths that already point into the root
    if path.startswith(str(ROOT)):
        path = path[len(str(ROOT)):]
    path = path.lstrip("/")
    resolved = (ROOT / path).resolve()
    if resolved != ROOT and ROOT not in resolved.parents:
        raise PermissionError(f"Pfad '{path}' liegt außerhalb des Second Brain.")
    return resolved


def _check_file(path: str) -> Path:
    p = _resolve(path)
    if p == ROOT:
        raise ValueError("Pfad zeigt auf den Stammordner, nicht auf eine Datei.")
    if p.suffix.lower() not in ALLOWED_EXT:
        raise ValueError(f"Dateityp '{p.suffix}' nicht erlaubt. Erlaubt: {sorted(ALLOWED_EXT)}")
    return p


def _check_writable() -> None:
    if READ_ONLY:
        raise PermissionError("Second Brain ist im Nur-Lese-Modus (Option read_only).")


def _in_trash(p: Path) -> bool:
    return TRASH_DIR in p.relative_to(ROOT).parts


def _iter_files(base: Path, include_trash: bool = False):
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in ALLOWED_EXT:
            continue
        if not include_trash and _in_trash(f):
            continue
        yield f


def _mtime(p: Path) -> str:
    return datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")


def _check_size(content: str) -> None:
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"Inhalt größer als {MAX_FILE_BYTES // 1024} KB.")

# ── Tools: read ───────────────────────────────────────────────────────────────

@mcp.tool()
def sb_overview() -> dict:
    """Überblick: Inhalt von README.md (Konventionen), Ordnerbaum (ohne Dateien) und Anzahl Notizen.
    Als Erstes aufrufen, bevor Notizen gesucht oder angelegt werden."""
    readme = ROOT / README
    folders = sorted(
        _rel(d) for d in ROOT.rglob("*")
        if d.is_dir() and not _in_trash(d) and not d.name.startswith(".")
    )
    return {
        "root": str(ROOT),
        "read_only": READ_ONLY,
        "readme": readme.read_text(encoding="utf-8") if readme.exists() else None,
        "folders": folders,
        "note_count": sum(1 for _ in _iter_files(ROOT)),
    }


@mcp.tool()
def sb_list(folder: str = "", recursive: bool = False) -> dict:
    """Listet Notizen und Unterordner. Args: folder (relativ, leer = Stamm), recursive."""
    base = _resolve(folder)
    if not base.is_dir():
        return {"success": False, "error": f"Ordner existiert nicht: {folder}"}
    entries = []
    items = base.rglob("*") if recursive else base.iterdir()
    for item in sorted(items):
        if _in_trash(item) or item.name.startswith("."):
            continue
        if item.is_dir():
            entries.append({"path": _rel(item), "type": "folder"})
        elif item.suffix.lower() in ALLOWED_EXT:
            entries.append({"path": _rel(item), "type": "file",
                            "size": item.stat().st_size, "modified": _mtime(item)})
    return {"success": True, "folder": _rel(base), "entries": entries}


@mcp.tool()
def sb_read(path: str) -> dict:
    """Liest eine Notiz. Args: path (relativ, z. B. '10_Projekte/hundebox.md')."""
    p = _check_file(path)
    if not p.is_file():
        return {"success": False, "error": f"Datei existiert nicht: {path}"}
    return {"success": True, "path": _rel(p), "modified": _mtime(p),
            "content": p.read_text(encoding="utf-8", errors="replace")}


@mcp.tool()
def sb_search(query: str, folder: str = "", max_results: int = 20) -> dict:
    """Volltextsuche (Groß/Klein egal) über Dateinamen und Inhalte.
    Mehrere Wörter = alle müssen in der Datei vorkommen. Liefert Trefferzeilen als Kontext.
    Args: query, folder (optional einschränken), max_results."""
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        return {"success": False, "error": "Leere Suche."}
    base = _resolve(folder)
    results = []
    for f in _iter_files(base):
        text = f.read_text(encoding="utf-8", errors="replace")
        hay = (_rel(f) + "\n" + text).lower()
        if not all(t in hay for t in terms):
            continue
        lines = text.splitlines()
        hits = [
            {"line": i + 1, "text": ln.strip()[:240]}
            for i, ln in enumerate(lines) if any(t in ln.lower() for t in terms)
        ][:5]
        score = sum(hay.count(t) for t in terms) + 5 * sum(t in _rel(f).lower() for t in terms)
        results.append({"path": _rel(f), "modified": _mtime(f), "score": score, "hits": hits})
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"success": True, "query": query, "total": len(results),
            "results": results[:max(1, max_results)]}


@mcp.tool()
def sb_recent(limit: int = 15) -> dict:
    """Zuletzt geänderte Notizen (neueste zuerst). Args: limit."""
    files = sorted(_iter_files(ROOT), key=lambda f: f.stat().st_mtime, reverse=True)
    return {"success": True,
            "notes": [{"path": _rel(f), "modified": _mtime(f)} for f in files[:max(1, limit)]]}

# ── Tools: write ──────────────────────────────────────────────────────────────

@mcp.tool()
def sb_write(path: str, content: str, overwrite: bool = False) -> dict:
    """Legt eine Notiz an (Unterordner werden erzeugt). Bestehende Datei nur mit overwrite=true.
    Für Ergänzungen besser sb_append oder sb_edit verwenden. Args: path, content, overwrite."""
    _check_writable()
    p = _check_file(path)
    if _in_trash(p):
        raise PermissionError("In .trash kann nicht geschrieben werden.")
    _check_size(content)
    if p.exists() and not overwrite:
        return {"success": False, "error": "Datei existiert bereits. sb_append/sb_edit nutzen oder overwrite=true."}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"success": True, "path": _rel(p), "bytes": len(content.encode("utf-8"))}


@mcp.tool()
def sb_append(path: str, content: str, heading: str = "") -> dict:
    """Hängt Text an eine Notiz an (legt sie an, falls nicht vorhanden).
    Mit heading (z. B. '## Log') wird der Text am Ende dieses Abschnitts eingefügt;
    existiert der Abschnitt nicht, wird er am Dateiende angelegt. Args: path, content, heading."""
    _check_writable()
    p = _check_file(path)
    if _in_trash(p):
        raise PermissionError("In .trash kann nicht geschrieben werden.")
    old = p.read_text(encoding="utf-8") if p.exists() else ""
    block = content.rstrip("\n") + "\n"

    if heading.strip():
        h = heading.strip()
        level = len(h) - len(h.lstrip("#")) or 2
        if not h.startswith("#"):
            h = "#" * level + " " + h
        lines = old.splitlines(keepends=True)
        idx = next((i for i, ln in enumerate(lines) if ln.strip() == h), None)
        if idx is None:
            new = old.rstrip("\n") + ("\n\n" if old.strip() else "") + h + "\n" + block
        else:
            end = len(lines)
            for j in range(idx + 1, len(lines)):
                m = re.match(r"^(#+)\s", lines[j])
                if m and len(m.group(1)) <= level:
                    end = j
                    break
            before = "".join(lines[:end]).rstrip("\n") + "\n"
            after = "".join(lines[end:])
            new = before + block + ("\n" + after if after else "")
    else:
        new = (old.rstrip("\n") + "\n" if old.strip() else "") + block

    _check_size(new)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(new, encoding="utf-8")
    return {"success": True, "path": _rel(p), "created": not old}


@mcp.tool()
def sb_edit(path: str, old_text: str, new_text: str) -> dict:
    """Ersetzt eine exakt einmal vorkommende Textstelle. Leerer new_text löscht sie.
    Args: path, old_text, new_text."""
    _check_writable()
    p = _check_file(path)
    if not p.is_file():
        return {"success": False, "error": f"Datei existiert nicht: {path}"}
    text = p.read_text(encoding="utf-8")
    n = text.count(old_text) if old_text else 0
    if n != 1:
        return {"success": False, "error": f"old_text kommt {n}-mal vor (muss genau 1-mal sein)."}
    new = text.replace(old_text, new_text, 1)
    _check_size(new)
    p.write_text(new, encoding="utf-8")
    return {"success": True, "path": _rel(p)}


@mcp.tool()
def sb_move(source: str, destination: str) -> dict:
    """Verschiebt/benennt eine Notiz um (überschreibt nie). Args: source, destination."""
    _check_writable()
    src = _check_file(source)
    dst = _check_file(destination)
    if not src.is_file():
        return {"success": False, "error": f"Quelle existiert nicht: {source}"}
    if dst.exists():
        return {"success": False, "error": f"Ziel existiert bereits: {destination}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return {"success": True, "source": _rel(src), "destination": _rel(dst)}


@mcp.tool()
def sb_delete(path: str) -> dict:
    """Verschiebt eine Notiz in den Papierkorb (.trash/<Zeitstempel>/...). Nichts wird endgültig gelöscht.
    Args: path."""
    _check_writable()
    p = _check_file(path)
    if not p.is_file():
        return {"success": False, "error": f"Datei existiert nicht: {path}"}
    if _in_trash(p):
        return {"success": False, "error": "Datei liegt bereits im Papierkorb."}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = ROOT / TRASH_DIR / stamp / p.relative_to(ROOT)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(p), str(dst))
    return {"success": True, "path": _rel(p), "trashed_to": _rel(dst)}

# ── Token auth middleware ─────────────────────────────────────────────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not TOKEN:
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        token_param = request.query_params.get("token", "")
        if auth_header == f"Bearer {TOKEN}" or token_param == TOKEN:
            return await call_next(request)
        return Response("Unauthorized", status_code=401)


app = mcp.streamable_http_app()
app.add_middleware(TokenAuthMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
