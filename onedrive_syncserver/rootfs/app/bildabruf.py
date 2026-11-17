#!/usr/bin/env python3
"""
Bildabruf fuer den OneDrive SyncServer (Konzept im Second Brain:
projekte/onedrive-bildabruf-konzept).

Dieses Modul wird von start_with_bildabruf.py in den unveraenderten
server.py eingehaengt. Faellt es beim Laden aus, laeuft der Sync-Server
ohne Bildabruf weiter (der Paperless-Sync ist davon nicht betroffen).

Enthaelt:
  - Endpunkte mit je genau einem Token und festen Ordnern. Berechtigungen
    je Aktion (Ansehen, Herunterladen, Schreiben, Loeschen): gesperrt,
    immer erlaubt oder nur mit Bestaetigung. Schreiben und Loeschen sind
    nicht gebaut und bleiben gesperrt.
  - Token-API auf EIGENEM Port (api_port, Standard 8776), getrennt vom
    Ingress-Port 8772:
      POST /v1/search            Dateiname -> Pfade (direkt bei OneDrive, ohne Suchindex)
      POST /v1/jobs              Download-Auftrag anlegen
      GET  /v1/jobs/<id>         Status (mit Wartezeit in Sekunden)
      GET  /v1/dl/<token>[/name] Datei ausliefern (eigener Download-Token je Datei)
      GET  /v1/info              Endpunkt-Infos
  - Ingress-Seite /bildabruf: Freigaben (Zulassen/Ablehnen mit Vorschau),
    Aufträge, Galerie der letzten Bilder, Berechtigungen. Nur erreichbar
    ueber Ingress (Supervisor-Netz) oder lokal (Loopback).
  - Worker mit Lebenszeichen-Pruefung: ohne Byte-Fortschritt seit 2 Minuten
    wird der Download einmal neu gestartet; nach 60 Minuten gesamt gilt der
    Auftrag als fehlgeschlagen.
"""

import calendar
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

from flask import Blueprint, Flask, Response, jsonify, request, send_file

VERSION = "1.0.0"

DATA_DIR = "/data/bildabruf"
ENDPOINTS_FILE = f"{DATA_DIR}/endpoints.json"
JOBS_FILE = f"{DATA_DIR}/jobs.json"
PREVIEW_DIR = f"{DATA_DIR}/previews"
FILES_DIR = f"{DATA_DIR}/files"
OPTIONS_FILE = "/data/options.json"

APPROVAL_TIMEOUT = 600          # Bestaetigung verfaellt nach 10 Minuten
PROCESSING_MAX = 3600           # harte Obergrenze fuer "wird bearbeitet"
STALL_SECONDS = 120             # kein Byte-Fortschritt seit 2 Minuten -> pruefen/neu starten
READY_RETENTION = 86400         # fertige Datei bleibt einen Tag bereit
FETCHED_KEEP = 600              # Endzustand nach einmaligem Abholen noch 10 Minuten sichtbar
MAX_WAITING_PER_ENDPOINT = 50
MAX_PARALLEL = 2
MAX_FOLDER_REQUESTS = 300       # Obergrenze Graph-Aufrufe je Suche
SEARCH_TIME_LIMIT = 40
GALLERY_INTERVAL = 900
CHUNK = 256 * 1024

MODES = ("blocked", "always", "confirm")
DEFAULT_PERMISSIONS = {"list": "always", "download": "confirm", "write": "blocked", "delete": "blocked"}
DEFAULT_ENDPOINTS = {
    "bilder": {
        "name": "Bilder (Suche und Abruf fuer Claude)",
        "folders": ["Bilder/Eigene Aufnahmen", "Bilder/Screenshots"],
    },
    "scanner": {
        "name": "Foto holen (Einkaufsscanner)",
        "folders": ["Bilder/Eigene Aufnahmen", "Bilder/Screenshots"],
    },
}
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".tif", ".tiff")

_lock = threading.RLock()
_jobs = {}
_endpoints = {}
_running = set()
_cancel = set()
_options = {}
_gallery = {"updated": None, "refreshing": False, "error": None, "items": []}
_get_token = None
_graph_get = None
_graph_base = ""


def log(msg):
    print(f"[Bildabruf] {msg}", flush=True)


def _now():
    return time.time()


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ── Persistenz ──────────────────────────────────────────────────────────────────

def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save_jobs():
    _write_json(JOBS_FILE, _jobs)


def _save_endpoints():
    _write_json(ENDPOINTS_FILE, {"endpoints": _endpoints})


def _load_state():
    global _jobs, _endpoints, _options
    for d in (DATA_DIR, PREVIEW_DIR, FILES_DIR):
        os.makedirs(d, exist_ok=True)
    _options = _read_json(OPTIONS_FILE, {})
    _endpoints = _read_json(ENDPOINTS_FILE, {}).get("endpoints", {})
    created = False
    for ep_id, d in DEFAULT_ENDPOINTS.items():
        if ep_id not in _endpoints:
            _endpoints[ep_id] = {
                "name": d["name"],
                "folders": list(d["folders"]),
                "token": secrets.token_urlsafe(24),
                "permissions": dict(DEFAULT_PERMISSIONS),
            }
            created = True
    if created:
        _save_endpoints()
    _jobs = _read_json(JOBS_FILE, {})
    # Nach einem Neustart: Auftraege in Bearbeitung werden vom Worker neu aufgenommen.
    for j in _jobs.values():
        if j.get("state") == "processing":
            j["last_progress"] = _now()
    _save_jobs()
    # Tokens stehen bei jedem Start im Log (wie bei den anderen Servern, bewusst akzeptiert).
    for ep_id, ep in _endpoints.items():
        log(f"Endpunkt '{ep_id}' ({ep['name']}) Token: {ep['token']}")


# ── Pfade und Namen ────────────────────────────────────────────────────────

_ILLEGAL = re.compile(r'[\x00-\x1f\\<>:"|?*]')


def _clean_path(p):
    p = (p or "").strip().strip("/")
    if not p or _ILLEGAL.search(p):
        return None
    parts = p.split("/")
    if any(x in ("", ".", "..") for x in parts):
        return None
    return "/".join(parts)


def _clean_name(n):
    n = (n or "").strip()
    if not n or len(n) > 255 or "/" in n or _ILLEGAL.search(n) or n in (".", ".."):
        return None
    return n


def _in_folders(path, folders):
    pl = path.lower()
    return any(pl.startswith(f.strip("/").lower() + "/") for f in folders)


def _safe_name(name):
    s = re.sub(r"[^\w.\- ()\[\]]", "_", name, flags=re.UNICODE).strip()
    if not s or s.startswith("."):
        s = "datei" + s
    return s[:200]


def _q(path):
    return urllib.parse.quote(path)


def _is_404(exc):
    return str(exc).startswith("Graph 404")


# ── Endpunkte, Token, Berechtigungen ───────────────────────────────────────

def _auth_endpoint():
    auth = request.headers.get("Authorization", "")
    tok = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("X-Api-Token", "").strip()
    if not tok:
        return None
    for ep_id, ep in _endpoints.items():
        if secrets.compare_digest(tok.encode(), ep["token"].encode()):
            return ep_id, ep
    return None


def _base_url():
    configured = (_options.get("download_base_url") or "").rstrip("/")
    return configured or request.host_url.rstrip("/")


# ── Graph: Suche und Elemente ───────────────────────────────────────────────

def _item_by_path(path):
    return _graph_get(_get_token(), f"/me/drive/root:/{_q(path)}")


def _children_pages(token, folder_path, select="id,name,size,folder,file,lastModifiedDateTime", extra=""):
    """Liefert alle Kinder eines Ordners seitenweise (Generator ueber Einzelelemente)."""
    path = f"/me/drive/root:/{_q(folder_path)}:/children?$select={select}&$top=999{extra}"
    while path:
        result = _graph_get(token, path)
        for item in result.get("value", []):
            yield item
        nxt = result.get("@odata.nextLink")
        path = nxt.replace(_graph_base, "", 1) if nxt else None


def _search(endpoint, filename):
    """Sucht einen Dateinamen rekursiv in den festen Ordnern (direkt bei OneDrive)."""
    token = _get_token()
    needle = filename.lower()
    found = {}
    requests_used = 0
    started = _now()
    truncated = False
    for folder in endpoint["folders"]:
        folder = folder.strip("/")
        queue = [folder]
        while queue:
            if requests_used >= MAX_FOLDER_REQUESTS or _now() - started > SEARCH_TIME_LIMIT:
                truncated = True
                break
            current = queue.pop(0)
            requests_used += 1
            try:
                for item in _children_pages(token, current):
                    name = item.get("name", "")
                    if "folder" in item:
                        queue.append(f"{current}/{name}")
                    elif name.lower() == needle and item["id"] not in found:
                        found[item["id"]] = {
                            "path": f"{current}/{name}",
                            "name": name,
                            "size": item.get("size", 0),
                            "modified": item.get("lastModifiedDateTime"),
                        }
            except Exception as e:
                if _is_404(e):
                    continue
                raise
    return list(found.values()), truncated, requests_used


# ── Auftraege ─────────────────────────────────────────────────────────────────────

def _wait_seconds(job):
    """Voraussichtliche Wartezeit bis zur naechsten Abfrage. Vorlaeufige Werte, nach Messung anpassen."""
    st = job["state"]
    if st == "waiting_for_approval":
        return 60
    if st == "processing":
        return max(3, int((job.get("size") or 0) / 2_000_000) + 3)
    return 0


def _public_job(job, base):
    out = {
        "job_id": job["id"],
        "status": job["state"],
        "name": job["name"],
        "path": job["path"],
        "size": job.get("size"),
        "wait_seconds": _wait_seconds(job),
    }
    if job["state"] == "processing":
        out["bytes_done"] = job.get("bytes", 0)
    if job["state"] == "waiting_for_approval":
        out["approval_expires_at"] = _iso(job["created"] + APPROVAL_TIMEOUT)
    if job.get("error"):
        out["error"] = job["error"]
    if job["state"] == "ready":
        out["download_url"] = f"{base}/v1/dl/{job['dl_token']}/{urllib.parse.quote(job['name'])}"
        out["sha256"] = job.get("sha256")
        out["expires_at"] = _iso(job["ready_at"] + READY_RETENTION)
    return out


def _delete_preview(job_id):
    try:
        os.remove(f"{PREVIEW_DIR}/{job_id}.jpg")
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Vorschau {job_id} nicht geloescht: {e}")
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["preview"] = False


def _delete_job_files(job_id):
    shutil.rmtree(f"{FILES_DIR}/{job_id}", ignore_errors=True)


def _fetch_preview(job_id, item_id):
    """Laedt ein Vorschaubild fuer die Freigabeseite (im Hintergrund, best effort)."""
    try:
        data = _graph_get(_get_token(), f"/me/drive/items/{item_id}/thumbnails")
        thumbs = data.get("value", [])
        url = None
        if thumbs:
            for size in ("large", "medium", "small"):
                if size in thumbs[0] and thumbs[0][size].get("url"):
                    url = thumbs[0][size]["url"]
                    break
        if not url:
            return
        with urllib.request.urlopen(url, timeout=20) as r:
            blob = r.read(8 * 1024 * 1024)
        with _lock:
            job = _jobs.get(job_id)
            if not job or job["state"] != "waiting_for_approval":
                return
        with open(f"{PREVIEW_DIR}/{job_id}.jpg", "wb") as f:
            f.write(blob)
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["preview"] = True
                _save_jobs()
    except Exception as e:
        log(f"Vorschau fuer Auftrag {job_id} nicht verfuegbar: {e}")


def decide(job_id, decision):
    """Zulassen oder Ablehnen eines wartenden Auftrags (nur ueber die Ingress-Seite)."""
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            raise KeyError("Auftrag nicht gefunden")
        if job["state"] != "waiting_for_approval":
            raise ValueError("Auftrag wartet nicht (mehr) auf Bestaetigung")
        if _now() - job["created"] > APPROVAL_TIMEOUT:
            job["state"] = "expired"
            _save_jobs()
            raise ValueError("Die Bestaetigung ist abgelaufen")
        if decision == "allow":
            job["state"] = "processing"
            job["decided"] = _now()
            job["started_processing"] = _now()
            job["last_progress"] = _now()
        else:
            job["state"] = "rejected"
            job["decided"] = _now()
        _save_jobs()
        result = job["state"]
    _delete_preview(job_id)
    log(f"Auftrag {job_id}: {result}")
    return result


class _Stalled(Exception):
    pass


def _fail(job, message):
    job["state"] = "failed"
    job["error"] = message
    job["ended"] = _now()
    _save_jobs()
    log(f"Auftrag {job['id']} fehlgeschlagen: {message}")


def _handle_failure(job_id, exc):
    with _lock:
        job = _jobs.get(job_id)
        _cancel.discard(job_id)
        if not job:
            return
        shutil.rmtree(f"{FILES_DIR}/{job_id}", ignore_errors=True)
        if job["state"] != "processing":
            return
        message = "Kein Fortschritt (haengt)" if isinstance(exc, _Stalled) else str(exc) or type(exc).__name__
        if job.get("restarts", 0) < 1:
            job["restarts"] = job.get("restarts", 0) + 1
            job["last_progress"] = _now()
            _save_jobs()
            log(f"Auftrag {job_id}: Neustart nach Fehler ({message})")
        else:
            _fail(job, f"{message} (nach einem Neustart)")


def _run_job(job_id):
    try:
        with _lock:
            job = _jobs.get(job_id)
            if not job or job["state"] != "processing":
                return
            job["bytes"] = 0
            job["last_progress"] = _now()
            job.setdefault("started_processing", _now())
            item_id, name = job["item_id"], job["name"]
            _save_jobs()
        _cancel.discard(job_id)
        token = _get_token()
        meta = _graph_get(token, f"/me/drive/items/{item_id}")
        url = meta.get("@microsoft.graph.downloadUrl")
        if not url:
            raise RuntimeError("Keine downloadUrl von Microsoft Graph erhalten")
        expected = meta.get("size")
        hashes = (meta.get("file") or {}).get("hashes") or {}
        dest_dir = f"{FILES_DIR}/{job_id}"
        os.makedirs(dest_dir, exist_ok=True)
        dest = f"{dest_dir}/{_safe_name(name)}"
        part = dest + ".part"
        sha256, sha1 = hashlib.sha256(), hashlib.sha1()
        done = 0
        with urllib.request.urlopen(url, timeout=30) as resp, open(part, "wb") as f:
            while True:
                if job_id in _cancel:
                    raise _Stalled()
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                sha256.update(chunk)
                sha1.update(chunk)
                done += len(chunk)
                with _lock:
                    j = _jobs.get(job_id)
                    if j:
                        j["bytes"] = done
                        j["last_progress"] = _now()
        if expected is not None and done != expected:
            raise RuntimeError(f"Groesse stimmt nicht: {done} statt {expected} Byte")
        if hashes.get("sha256Hash") and hashes["sha256Hash"].lower() != sha256.hexdigest():
            raise RuntimeError("Pruefsumme (SHA-256) stimmt nicht mit OneDrive ueberein")
        if not hashes.get("sha256Hash") and hashes.get("sha1Hash") and hashes["sha1Hash"].lower() != sha1.hexdigest():
            raise RuntimeError("Pruefsumme (SHA-1) stimmt nicht mit OneDrive ueberein")
        os.replace(part, dest)
        with _lock:
            j = _jobs.get(job_id)
            if j and j["state"] == "processing":
                j["state"] = "ready"
                j["ready_at"] = _now()
                j["dl_token"] = secrets.token_urlsafe(24)
                j["sha256"] = sha256.hexdigest()
                j["local_file"] = os.path.basename(dest)
                j["bytes"] = done
                _save_jobs()
                log(f"Auftrag {job_id}: bereit ({done} Byte)")
            else:
                shutil.rmtree(dest_dir, ignore_errors=True)
    except Exception as e:
        _handle_failure(job_id, e)
    finally:
        with _lock:
            _running.discard(job_id)


def _worker_loop():
    while True:
        try:
            with _lock:
                todo = [j["id"] for j in _jobs.values() if j["state"] == "processing" and j["id"] not in _running]
                for jid in todo:
                    if len(_running) >= MAX_PARALLEL:
                        break
                    _running.add(jid)
                    threading.Thread(target=_run_job, args=(jid,), daemon=True).start()
        except Exception:
            log("Worker-Fehler:\n" + traceback.format_exc())
        time.sleep(1)


def _watchdog_loop():
    """Lebenszeichen-Pruefung: haengende Auftraege erkennen (Neustart/Abbruch)."""
    while True:
        time.sleep(20)
        try:
            now = _now()
            with _lock:
                for j in list(_jobs.values()):
                    if j["state"] != "processing":
                        continue
                    if now - j.get("started_processing", now) > PROCESSING_MAX:
                        _cancel.add(j["id"])
                        _fail(j, "Zeitlimit von 60 Minuten ueberschritten")
                    elif j["id"] in _running and now - j.get("last_progress", now) > STALL_SECONDS:
                        _cancel.add(j["id"])
                        log(f"Auftrag {j['id']}: seit {STALL_SECONDS} s kein Fortschritt, Abbruch zum Neustart")
        except Exception:
            log("Watchdog-Fehler:\n" + traceback.format_exc())


def _cleanup_loop():
    while True:
        time.sleep(60)
        try:
            now = _now()
            with _lock:
                for j in list(_jobs.values()):
                    st = j["state"]
                    if st == "waiting_for_approval" and now - j["created"] > APPROVAL_TIMEOUT:
                        j["state"] = "expired"
                        j["ended"] = now
                        log(f"Auftrag {j['id']}: abgelaufen (keine Bestaetigung)")
                    elif st == "ready" and now - j["ready_at"] > READY_RETENTION:
                        _delete_job_files(j["id"])
                        del _jobs[j["id"]]
                        log(f"Auftrag {j['id']}: Datei nach einem Tag geloescht")
                    elif st in ("rejected", "expired", "failed"):
                        fetched = j.get("fetched_at")
                        if (fetched and now - fetched > FETCHED_KEEP) or now - j.get("ended", j.get("decided", j["created"])) > READY_RETENTION:
                            del _jobs[j["id"]]
                _save_jobs()
                known = set(_jobs)
                waiting = {i for i, j in _jobs.items() if j["state"] == "waiting_for_approval"}
            for fn in os.listdir(PREVIEW_DIR):
                if fn.endswith(".jpg") and fn[:-4] not in waiting:
                    _delete_preview(fn[:-4])
            for d in os.listdir(FILES_DIR):
                path = f"{FILES_DIR}/{d}"
                if d not in known and now - os.path.getmtime(path) > 300:
                    shutil.rmtree(path, ignore_errors=True)
        except Exception:
            log("Aufraeum-Fehler:\n" + traceback.format_exc())


# ── Galerie "letzte Bilder" ───────────────────────────────────────────────

def _to_epoch(s):
    try:
        return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0


def _gallery_refresh():
    with _lock:
        if _gallery["refreshing"]:
            return
        _gallery["refreshing"] = True
    try:
        days = int(_options.get("gallery_days", 10) or 10)
        cutoff = _now() - days * 86400
        folders = sorted({f.strip("/") for ep in _endpoints.values() for f in ep["folders"]})
        token = _get_token()
        items = []
        select = "id,name,size,file,folder,lastModifiedDateTime,thumbnails"
        for folder in folders:
            sorted_ok = True
            try:
                gen = _children_pages(token, folder, select, "&$orderby=lastModifiedDateTime%20desc&$expand=thumbnails")
                first = next(gen, None)
                stream = ([first] if first else []) + list(gen) if False else None
            except Exception:
                sorted_ok = False
            if sorted_ok:
                gen = _children_pages(token, folder, select, "&$orderby=lastModifiedDateTime%20desc&$expand=thumbnails")
            else:
                gen = _children_pages(token, folder, select, "&$expand=thumbnails")
            for item in gen:
                if "folder" in item:
                    continue
                mtime = _to_epoch(item.get("lastModifiedDateTime", ""))
                if mtime < cutoff:
                    if sorted_ok:
                        break
                    continue
                name = item.get("name", "")
                mime = (item.get("file") or {}).get("mimeType", "")
                if not (mime.startswith("image/") or name.lower().endswith(IMAGE_EXT)):
                    continue
                thumb = None
                th = item.get("thumbnails") or []
                if th:
                    for size in ("small", "medium"):
                        if size in th[0] and th[0][size].get("url"):
                            thumb = th[0][size]["url"]
                            break
                items.append({"name": name, "path": f"{folder}/{name}", "mtime": mtime, "thumb": thumb, "size": item.get("size", 0)})
        items.sort(key=lambda x: x["mtime"], reverse=True)
        with _lock:
            _gallery.update(items=items[:300], updated=_now(), error=None)
    except Exception as e:
        log(f"Galerie-Abgleich fehlgeschlagen: {e}")
        with _lock:
            _gallery["error"] = str(e)
    finally:
        with _lock:
            _gallery["refreshing"] = False


def _gallery_loop():
    while True:
        _gallery_refresh()
        time.sleep(GALLERY_INTERVAL)


# ── Token-API (eigener Port) ────────────────────────────────────────────────

def _make_api_app():
    api = Flask("bildabruf_api")

    @api.after_request
    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _err(msg, code):
        return jsonify({"success": False, "error": msg}), code

    @api.route("/v1/health")
    def health():
        return jsonify({"success": True, "service": "bildabruf", "version": VERSION})

    @api.route("/v1/info")
    def info():
        auth = _auth_endpoint()
        if not auth:
            return _err("Token fehlt oder ungueltig", 401)
        ep_id, ep = auth
        return jsonify({"success": True, "endpoint": ep_id, "name": ep["name"], "folders": ep["folders"], "permissions": ep["permissions"]})

    @api.route("/v1/search", methods=["POST"])
    def search():
        auth = _auth_endpoint()
        if not auth:
            return _err("Token fehlt oder ungueltig", 401)
        ep_id, ep = auth
        if ep["permissions"].get("list") != "always":
            return _err("Ansehen ist fuer diesen Endpunkt gesperrt", 403)
        name = _clean_name((request.get_json(silent=True) or {}).get("filename"))
        if not name:
            return _err("filename fehlt oder ist ungueltig (kein Pfad erlaubt)", 400)
        try:
            locations, truncated, used = _search(ep, name)
        except Exception as e:
            log(f"Suche fehlgeschlagen: {e}")
            return _err(f"OneDrive-Abfrage fehlgeschlagen: {e}", 502)
        return jsonify({"success": True, "found": len(locations), "locations": locations, "search_incomplete": truncated, "folders_searched": used})

    @api.route("/v1/jobs", methods=["POST"])
    def create_job():
        auth = _auth_endpoint()
        if not auth:
            return _err("Token fehlt oder ungueltig", 401)
        ep_id, ep = auth
        mode = ep["permissions"].get("download", "blocked")
        if mode not in ("always", "confirm"):
            return _err("Herunterladen ist fuer diesen Endpunkt gesperrt", 403)
        data = request.get_json(silent=True) or {}
        if data.get("action", "download") != "download":
            return _err("Nur die Aktion 'download' ist moeglich", 400)
        path = _clean_path(data.get("path"))
        if not path or not _in_folders(path, ep["folders"]):
            return _err("path ungueltig oder ausserhalb der Ordner dieses Endpunkts", 400)
        with _lock:
            for j in _jobs.values():
                if j["endpoint"] == ep_id and j["path"].lower() == path.lower() and j["state"] in ("waiting_for_approval", "processing", "ready"):
                    return jsonify({"success": True, **_public_job(j, _base_url())})
            waiting = sum(1 for j in _jobs.values() if j["endpoint"] == ep_id and j["state"] == "waiting_for_approval")
        if mode == "confirm" and waiting >= MAX_WAITING_PER_ENDPOINT:
            return _err(f"Zu viele Auftraege warten auf Bestaetigung (Grenze {MAX_WAITING_PER_ENDPOINT})", 429)
        try:
            item = _item_by_path(path)
        except Exception as e:
            if _is_404(e):
                return _err("Datei nicht gefunden", 404)
            log(f"Auftrag: OneDrive-Abfrage fehlgeschlagen: {e}")
            return _err(f"OneDrive-Abfrage fehlgeschlagen: {e}", 502)
        if "file" not in item:
            return _err("path ist keine Datei", 400)
        job_id = secrets.token_hex(6)
        now = _now()
        job = {
            "id": job_id, "endpoint": ep_id, "path": path, "name": item.get("name", path.rsplit("/", 1)[-1]),
            "item_id": item["id"], "size": item.get("size", 0), "created": now, "bytes": 0, "restarts": 0,
            "state": "waiting_for_approval" if mode == "confirm" else "processing", "preview": False,
        }
        if mode == "always":
            job["decided"] = now
            job["started_processing"] = now
            job["last_progress"] = now
        with _lock:
            _jobs[job_id] = job
            _save_jobs()
        log(f"Auftrag {job_id}: {job['state']} ({ep_id}, {path})")
        if mode == "confirm":
            threading.Thread(target=_fetch_preview, args=(job_id, item["id"]), daemon=True).start()
        return jsonify({"success": True, **_public_job(job, _base_url())}), 201

    @api.route("/v1/jobs/<job_id>")
    def job_status(job_id):
        auth = _auth_endpoint()
        if not auth:
            return _err("Token fehlt oder ungueltig", 401)
        ep_id, _ = auth
        with _lock:
            job = _jobs.get(job_id)
            if not job or job["endpoint"] != ep_id:
                return _err("Auftrag nicht gefunden (abgeholt, abgelaufen oder unbekannt)", 404)
            if job["state"] in ("rejected", "expired", "failed") and not job.get("fetched_at"):
                job["fetched_at"] = _now()
                _save_jobs()
            return jsonify({"success": True, **_public_job(job, _base_url())})

    @api.route("/v1/dl/<dl_token>")
    @api.route("/v1/dl/<dl_token>/<path:_name>")
    def download(dl_token, _name=None):
        with _lock:
            match = None
            for j in _jobs.values():
                if j["state"] == "ready" and j.get("dl_token") and secrets.compare_digest(dl_token.encode(), j["dl_token"].encode()):
                    match = dict(j)
                    break
        if not match or _now() - match["ready_at"] > READY_RETENTION:
            return _err("Link ungueltig oder abgelaufen", 404)
        path = f"{FILES_DIR}/{match['id']}/{match['local_file']}"
        if not os.path.exists(path):
            return _err("Datei nicht mehr vorhanden", 404)
        return send_file(path, as_attachment=True, download_name=match["name"], conditional=True)

    return api


# ── Ingress-Seite (nur ueber Ingress oder lokal) ─────────────────────────────────────

_ALLOWED_NETS = [
    ipaddress.ip_network("172.30.32.0/23"),   # Supervisor-/Ingress-Netz
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
]

bp = Blueprint("bildabruf_ui", __name__)


@bp.before_request
def _guard():
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return Response("Zugriff nur ueber Home Assistant", status=403)
    if not any(ip in n for n in _ALLOWED_NETS):
        return Response("Zugriff nur ueber Home Assistant (Ingress)", status=403)
    return None


def _ui_base():
    return request.headers.get("X-Ingress-Path", "").rstrip("/")


@bp.route("/bildabruf")
def page():
    threading.Thread(target=_gallery_refresh, daemon=True).start()
    return Response(PAGE_HTML.replace("__BASE__", _ui_base()), mimetype="text/html")


@bp.route("/bildabruf/api/state")
def ui_state():
    with _lock:
        jobs = []
        for j in sorted(_jobs.values(), key=lambda x: x["created"], reverse=True):
            jobs.append({
                "id": j["id"], "endpoint": j["endpoint"], "endpoint_name": _endpoints.get(j["endpoint"], {}).get("name", j["endpoint"]),
                "path": j["path"], "name": j["name"], "size": j.get("size"), "state": j["state"], "created": j["created"],
                "preview": bool(j.get("preview")), "bytes": j.get("bytes", 0), "error": j.get("error"),
                "ready_at": j.get("ready_at"),
            })
        endpoints = [{"id": i, "name": e["name"], "folders": e["folders"], "permissions": e["permissions"]} for i, e in _endpoints.items()]
        gallery = dict(_gallery)
    return jsonify({"now": _now(), "approval_timeout": APPROVAL_TIMEOUT, "jobs": jobs, "endpoints": endpoints, "gallery": gallery, "modes": MODES})


@bp.route("/bildabruf/preview/<job_id>")
def ui_preview(job_id):
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return Response(status=404)
    path = f"{PREVIEW_DIR}/{job_id}.jpg"
    if not os.path.exists(path):
        return Response(status=404)
    return send_file(path, mimetype="image/jpeg")


@bp.route("/bildabruf/api/decide", methods=["POST"])
def ui_decide():
    data = request.get_json(silent=True) or {}
    if data.get("decision") not in ("allow", "reject"):
        return jsonify({"success": False, "error": "decision muss allow oder reject sein"}), 400
    try:
        state = decide(str(data.get("job_id", "")), data["decision"])
        return jsonify({"success": True, "state": state})
    except KeyError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 409


@bp.route("/bildabruf/api/permissions", methods=["POST"])
def ui_permissions():
    data = request.get_json(silent=True) or {}
    ep_id, action, mode = data.get("endpoint"), data.get("action"), data.get("mode")
    if ep_id not in _endpoints or action not in ("list", "download"):
        return jsonify({"success": False, "error": "Endpunkt oder Aktion unbekannt (Schreiben und Loeschen sind gesperrt)"}), 400
    allowed = ("blocked", "always") if action == "list" else MODES
    if mode not in allowed:
        return jsonify({"success": False, "error": f"Einstellung nicht erlaubt (moeglich: {', '.join(allowed)})"}), 400
    with _lock:
        _endpoints[ep_id]["permissions"][action] = mode
        _save_endpoints()
    log(f"Berechtigung {ep_id}/{action} = {mode}")
    return jsonify({"success": True})


@bp.route("/bildabruf/api/gallery_refresh", methods=["POST"])
def ui_gallery_refresh():
    threading.Thread(target=_gallery_refresh, daemon=True).start()
    return jsonify({"success": True})


@bp.route("/bildabruf/api/delete_job", methods=["POST"])
def ui_delete_job():
    """Entfernt einen Auftrag samt Datei sofort (Aufraeumen von Hand)."""
    job_id = str((request.get_json(silent=True) or {}).get("job_id", ""))
    with _lock:
        job = _jobs.pop(job_id, None)
        _cancel.add(job_id)
        _save_jobs()
    if not job:
        return jsonify({"success": False, "error": "Auftrag nicht gefunden"}), 404
    _delete_job_files(job_id)
    _delete_preview(job_id)
    return jsonify({"success": True})


def _inject_link(resp):
    """Setzt auf der bestehenden Startseite einen Link zur Bildabruf-Seite ein."""
    try:
        if request.path in ("/", "") and resp.mimetype == "text/html" and resp.status_code == 200:
            body = resp.get_data(as_text=True)
            link = (
                '<div style="padding:8px 16px;background:#e8f0fe;font-family:sans-serif">'
                f'<a href="{_ui_base()}/bildabruf">Bildabruf: Freigaben, Galerie und Endpunkte</a></div>'
            )
            if "<body>" in body and "bildabruf" not in body[:2000]:
                resp.set_data(body.replace("<body>", "<body>" + link, 1))
    except Exception:
        pass
    return resp


PAGE_HTML = r'''<!DOCTYPE html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bildabruf</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#f5f6f8;color:#1b1f24}
 h1{font-size:20px;margin:0 0 12px} h2{font-size:16px;margin:24px 0 8px}
 .card{background:#fff;border-radius:10px;padding:12px;margin:8px 0;box-shadow:0 1px 3px rgba(0,0,0,.12);display:flex;gap:12px;align-items:flex-start}
 .card img.prev{width:140px;max-height:140px;object-fit:contain;background:#eee;border-radius:6px}
 .grow{flex:1;min-width:0;word-break:break-all}
 button{border:0;border-radius:6px;padding:8px 14px;font-size:14px;cursor:pointer;margin-right:6px}
 .ok{background:#2e7d32;color:#fff} .no{background:#c62828;color:#fff} .pl{background:#dde3ea}
 .muted{color:#667;font-size:13px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px}
 .tile{background:#fff;border-radius:8px;padding:4px;cursor:pointer;font-size:11px;word-break:break-all}
 .tile img{width:100%;height:90px;object-fit:cover;border-radius:6px;background:#ddd}
 table{border-collapse:collapse;width:100%;background:#fff;border-radius:8px} td,th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left;font-size:13px}
 .toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:#222;color:#fff;padding:8px 14px;border-radius:8px;display:none}
 @media (prefers-color-scheme:dark){body{background:#14171a;color:#e6e9ed}.card,table,.tile{background:#1f242a}.pl{background:#39424c;color:#fff}.muted{color:#9aa4ae}td,th{border-color:#333}}
</style></head><body>
<h1>Bildabruf</h1>
<h2>Freigaben</h2><div id="approvals" class="muted">Lade ...</div>
<h2>Auftraege</h2><div id="jobs" class="muted"></div>
<h2>Letzte Bilder <span id="gstate" class="muted"></span> <button class="pl" onclick="refreshGallery()">Jetzt pruefen</button></h2>
<div class="muted">Klick auf ein Bild kopiert den Dateinamen.</div><div id="gallery" class="grid"></div>
<h2>Endpunkte und Berechtigungen</h2><div id="endpoints"></div>
<div class="toast" id="toast"></div>
<script>
const BASE = "__BASE__";
const LABEL = {blocked:"gesperrt", always:"immer erlaubt", confirm:"nur mit Bestaetigung"};
const STATE = {waiting_for_approval:"wartet auf Bestaetigung", processing:"wird bearbeitet", ready:"bereit", rejected:"abgelehnt", expired:"abgelaufen", failed:"fehlgeschlagen"};
function el(tag, props, kids){const e=document.createElement(tag);for(const k in (props||{})){if(k==='text')e.textContent=props[k];else if(k.startsWith('on'))e.addEventListener(k.slice(2),props[k]);else e.setAttribute(k,props[k]);}(kids||[]).forEach(c=>e.appendChild(c));return e;}
function fmt(n){if(n==null)return '';if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(0)+' KB';return (n/1048576).toFixed(1)+' MB';}
function toast(t){const x=document.getElementById('toast');x.textContent=t;x.style.display='block';setTimeout(()=>x.style.display='none',2000);}
async function post(path, body){const r=await fetch(BASE+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json();}
async function decide(id, d){const r=await post('/bildabruf/api/decide',{job_id:id,decision:d});if(!r.success)toast(r.error);load();}
async function setPerm(ep, action, mode){const r=await post('/bildabruf/api/permissions',{endpoint:ep,action:action,mode:mode});toast(r.success?'Gespeichert':r.error);load();}
async function refreshGallery(){await post('/bildabruf/api/gallery_refresh');toast('Pruefung gestartet');setTimeout(load,1500);}
function copyName(n){const t=document.createElement('textarea');t.value=n;document.body.appendChild(t);t.select();try{document.execCommand('copy');toast('Kopiert: '+n);}catch(e){window.prompt('Dateiname:',n);}document.body.removeChild(t);}
function render(s){
 const ap=document.getElementById('approvals');ap.textContent='';
 const waiting=s.jobs.filter(j=>j.state==='waiting_for_approval');
 if(!waiting.length)ap.appendChild(el('div',{class:'muted',text:'Keine offenen Anfragen.'}));
 waiting.forEach(j=>{const left=Math.max(0,Math.round(s.approval_timeout-(s.now-j.created)));
  const kids=[];if(j.preview)kids.push(el('img',{class:'prev',src:BASE+'/bildabruf/preview/'+j.id}));
  kids.push(el('div',{class:'grow'},[el('div',{text:j.endpoint_name+': Herunterladen'}),el('div',{text:j.path}),el('div',{class:'muted',text:fmt(j.size)+' - noch '+left+' s'}),
   el('div',{style:'margin-top:8px'},[el('button',{class:'ok',text:'Zulassen',onclick:()=>decide(j.id,'allow')}),el('button',{class:'no',text:'Ablehnen',onclick:()=>decide(j.id,'reject')})])]));
  ap.appendChild(el('div',{class:'card'},kids));});
 const jb=document.getElementById('jobs');jb.textContent='';
 const others=s.jobs.filter(j=>j.state!=='waiting_for_approval');
 if(!others.length)jb.appendChild(el('div',{class:'muted',text:'Keine Auftraege.'}));
 others.forEach(j=>{let t=STATE[j.state]||j.state;if(j.state==='processing')t+=' ('+fmt(j.bytes)+' von '+fmt(j.size)+')';if(j.error)t+=': '+j.error;
  jb.appendChild(el('div',{class:'card'},[el('div',{class:'grow'},[el('div',{text:j.path}),el('div',{class:'muted',text:t}),el('button',{class:'pl',text:'Entfernen',onclick:async()=>{await post('/bildabruf/api/delete_job',{job_id:j.id});load();}})])]));});
 const g=s.gallery;document.getElementById('gstate').textContent=g.refreshing?'(Abgleich laeuft ...)':(g.updated?'(Stand '+new Date(g.updated*1000).toLocaleTimeString()+')':'')+(g.error?' Fehler: '+g.error:'');
 const gr=document.getElementById('gallery');gr.textContent='';
 g.items.forEach(i=>{const kids=[];if(i.thumb)kids.push(el('img',{src:i.thumb,loading:'lazy'}));kids.push(el('div',{text:i.name}));gr.appendChild(el('div',{class:'tile',onclick:()=>copyName(i.name)},kids));});
 const ep=document.getElementById('endpoints');ep.textContent='';
 const rows=[el('tr',{},['Endpunkt','Ordner','Ansehen','Herunterladen','Schreiben','Loeschen'].map(h=>el('th',{text:h})))];
 s.endpoints.forEach(e=>{const sel=(action,modes)=>{const x=el('select',{},modes.map(m=>{const o=el('option',{value:m,text:LABEL[m]});if(e.permissions[action]===m)o.selected=true;return o;}));x.addEventListener('change',()=>setPerm(e.id,action,x.value));return x;};
  rows.push(el('tr',{},[el('td',{text:e.name}),el('td',{text:e.folders.join(', ')}),el('td',{},[sel('list',['always','blocked'])]),el('td',{},[sel('download',['confirm','always','blocked'])]),el('td',{text:'gesperrt'}),el('td',{text:'gesperrt'})]));});
 ep.appendChild(el('table',{},rows));
}
async function load(){try{const r=await fetch(BASE+'/bildabruf/api/state');render(await r.json());}catch(e){}}
load();setInterval(load,4000);
</script></body></html>'''


# ── Einstieg ───────────────────────────────────────────────────────────────────────

def setup(ui_app, get_access_token, graph_get, graph_base):
    """Haengt Ingress-Seite ein und startet Token-API und Hintergrund-Threads."""
    global _get_token, _graph_get, _graph_base
    _get_token, _graph_get, _graph_base = get_access_token, graph_get, graph_base
    _load_state()
    ui_app.register_blueprint(bp)
    ui_app.after_request(_inject_link)
    api_port = int(_options.get("api_port", 8776) or 8776)
    api = _make_api_app()
    threading.Thread(
        target=lambda: api.run(host="0.0.0.0", port=api_port, debug=False, threaded=True, use_reloader=False),
        daemon=True,
    ).start()
    for fn in (_worker_loop, _watchdog_loop, _cleanup_loop, _gallery_loop):
        threading.Thread(target=fn, daemon=True).start()
    log(f"Bildabruf {VERSION} gestartet (Token-API Port {api_port}, Ingress-Seite /bildabruf)")
