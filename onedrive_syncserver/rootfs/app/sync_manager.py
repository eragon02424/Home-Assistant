#!/usr/bin/env python3
"""OneDrive SyncServer - Sync Manager

Fuehrt den eigentlichen Sync durch:
0. Prueft eine Lock-Datei um zu verhindern dass zwei Sync-Laeufe
   gleichzeitig laufen (z.B. manueller Klick waehrend der 5-Minuten-Timer
   ausloest) - das kann sonst zu SQLite-Konflikten und haengenden Prozessen
   fuehren.
1. Ermittelt ALLE Ordner (rekursiv) via Graph API und schreibt eine
   sync_list Datei mit Include/Exclude-Zeilen an den "Grenzen" zwischen
   aktivierten und deaktivierten Aesten - so werden abgewaehlte Ordner
   (auch verschachtelt) GAR NICHT erst heruntergeladen.
   Wenn sich die sync_list gegenueber dem letzten Lauf geaendert hat,
   verlangt onedrive einen --resync (Sicherheitsmechanismus gegen
   ungewollten Datenverlust) - das wird automatisch erkannt und mit
   --resync --resync-auth bestaetigt, aber NUR wenn wirklich noetig.
2. onedrive --synchronize (OneDrive ist Master, no-remote-delete fuer
   lokale Loeschungen)
3. Filtert Dateien innerhalb synchronisierter Ordner nach Dateityp/Alter
4. Schreibt Status in sync_status.json
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

CONFIG_DIR = "/data"
SYNC_CONFIG = f"{CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR = f"{CONFIG_DIR}/onedrive"
SYNC_LIST_FILE = f"{ONEDRIVE_CONFIG_DIR}/sync_list"
SYNC_LIST_HASH_FILE = f"{CONFIG_DIR}/sync_list.hash"
LOCK_FILE = f"{CONFIG_DIR}/sync.lock"
SHARE_DIR = "/share/onedrive"
STATUS_FILE = f"{CONFIG_DIR}/sync_status.json"

CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "Files.ReadWrite Files.ReadWrite.All Sites.ReadWrite.All offline_access"

MAX_FOLDER_DEPTH = 10  # Sicherheitsgrenze gegen extrem tiefe Baeume
LOCK_STALE_SECONDS = 900  # Lock aelter als 15min = vermutlich verwaister Prozess

FILTER_EXTENSIONS = {
    "all": None,  # None = kein Filter
    "pdf": [".pdf"],
    "images": [".jpg", ".jpeg", ".png", ".tiff", ".tif", ".heic", ".webp"],
    "pdf_images": [".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif", ".heic", ".webp"],
    "office": [".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"],
}

DELETE_DAYS = {
    "never": None,
    "1d": 1,
    "7d": 7,
    "30d": 30,
    "180d": 180,
    "365d": 365,
}

def log(msg):
    """print() mit sofortigem Flush - sonst puffert Python alle Ausgaben
    komplett wenn stdout kein Terminal ist (z.B. docker logs), und man
    sieht erst am Ende was passiert ist."""
    print(msg, flush=True)

def load_config():
    if os.path.exists(SYNC_CONFIG):
        with open(SYNC_CONFIG) as f:
            return json.load(f)
    return {}

def save_status(status):
    with open(STATUS_FILE, 'w') as f:
        json.dump(status, f, indent=2)

def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"last_sync": None, "files_synced": 0, "errors": [], "authenticated": False}


def acquire_lock():
    """
    Verhindert zwei gleichzeitige Sync-Laeufe (z.B. manueller Klick
    waehrend der periodische Timer in run.sh ausloest). Ein Lock aelter
    als LOCK_STALE_SECONDS wird als verwaist betrachtet und ignoriert.
    """
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < LOCK_STALE_SECONDS:
            log(f"[LOCK] Ein anderer Sync laeuft bereits (Lock ist {age:.0f}s alt) - breche ab")
            return False
        log(f"[LOCK] Alter Lock ({age:.0f}s) wird als verwaist betrachtet und ueberschrieben")
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    return True

def release_lock():
    if os.path.exists(LOCK_FILE):
        os.remove(LOCK_FILE)


# --- Graph API Hilfsfunktionen (eigenstaendig, da separater Prozess) ---

def ms_post(url, data):
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        return None, body

def get_access_token():
    rt_file = f"{ONEDRIVE_CONFIG_DIR}/refresh_token"
    if not os.path.exists(rt_file):
        raise Exception("Nicht authentifiziert")
    with open(rt_file) as f:
        refresh_token = f.read().strip()
    result, err = ms_post(TOKEN_URL, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": SCOPE,
    })
    if err:
        raise Exception(f"Token-Fehler: {err.get('error_description', err.get('error'))}")
    if "refresh_token" in result:
        with open(rt_file, 'w') as f:
            f.write(result["refresh_token"])
    return result["access_token"]

def graph_get_children(token, item_id):
    url = f"{GRAPH_BASE}/me/drive/items/{item_id}/children?$select=id,name,folder"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())

def list_all_folders():
    """
    Listet ALLE Ordner rekursiv per Graph API (Pfad-Format wie lokal:
    'Top/Sub/Sub2'). Eigenstaendig implementiert (separater Prozess von
    server.py).
    """
    token = get_access_token()
    folders = []

    def walk(item_id, path, depth):
        if depth > MAX_FOLDER_DEPTH:
            return
        result = graph_get_children(token, item_id)
        for item in result.get("value", []):
            if "folder" in item:
                child_path = f"{path}/{item['name']}" if path else item['name']
                folders.append(child_path)
                if item["folder"].get("childCount", 0) > 0:
                    walk(item["id"], child_path, depth + 1)

    walk("root", "", 0)
    return sorted(folders)


def resolve_enabled(path, config):
    """Loest vererbte 'sync' Einstellung fuer einen Pfad auf (wie get_effective_config,
    aber nur fuer das sync-Flag)."""
    parts = path.split("/")
    enabled = True
    for i in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:i])
        if ancestor in config and "sync" in config[ancestor]:
            enabled = config[ancestor]["sync"]
    return enabled


def write_sync_list(config, all_folders):
    """
    Schreibt eine onedrive sync_list Datei mit Include/Exclude-Zeilen.
    Gibt zurueck ob sich der Inhalt gegenueber dem letzten Lauf geaendert
    hat (per Hash-Vergleich) - das entscheidet ob ein --resync noetig ist.
    """
    changed = False

    if not all_folders:
        return changed

    top_level_folders = [f for f in all_folders if "/" not in f]
    includes = []
    excludes = []
    for path in all_folders:
        cur_enabled = resolve_enabled(path, config)
        parts = path.split("/")
        if len(parts) == 1:
            parent_enabled = False
        else:
            parent_enabled = resolve_enabled("/".join(parts[:-1]), config)
        if cur_enabled and not parent_enabled:
            includes.append(f"{path}/*")
        elif not cur_enabled and parent_enabled:
            excludes.append(f"!{path}/*")

    if not excludes and len(includes) == len(top_level_folders):
        new_content = ""
    else:
        new_content = "\n".join(includes + excludes) + "\n"

    new_hash = hashlib.sha256(new_content.encode()).hexdigest()
    old_hash = None
    if os.path.exists(SYNC_LIST_HASH_FILE):
        with open(SYNC_LIST_HASH_FILE) as f:
            old_hash = f.read().strip()

    if new_hash != old_hash:
        changed = True
        with open(SYNC_LIST_HASH_FILE, 'w') as f:
            f.write(new_hash)

    if not new_content:
        if os.path.exists(SYNC_LIST_FILE):
            os.remove(SYNC_LIST_FILE)
        log("[sync_list] Alle Ordner aktiv - keine Einschraenkung")
    else:
        with open(SYNC_LIST_FILE, "w") as f:
            f.write(new_content)
        log(f"[sync_list] {len(includes)} Einschluss-, {len(excludes)} Ausschluss-Regeln geschrieben")

    if changed:
        log("[sync_list] Aenderung erkannt - dieser Sync laeuft mit --resync")

    return changed


def run_onedrive_sync(need_resync):
    """Run onedrive sync with OneDrive as master."""
    cmd = [
        "onedrive",
        "--confdir", ONEDRIVE_CONFIG_DIR,
        "--synchronize",
        "--no-remote-delete",
        "--verbose"
    ]
    if need_resync:
        cmd += ["--resync", "--resync-auth"]
    log(f"[onedrive] Starte: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )
        output_lines = []
        for line in proc.stdout:
            log(line.rstrip())
            output_lines.append(line)
        proc.wait(timeout=600)
        if proc.returncode != 0:
            err_msg = "".join(output_lines[-30:]).strip() or f"Exit-Code {proc.returncode} ohne Ausgabe"
            log(f"[WARN] onedrive exited with {proc.returncode}")
            return False, err_msg
        return True, None
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "Sync timeout after 600s"
    except Exception as e:
        return False, str(e)

def get_effective_config(folder_path, config):
    """Resolves inherited config for a folder."""
    parts = folder_path.split('/')
    effective = {
        "sync": True,
        "filter": "all",
        "delete_after": "never",
        "custom_local_path": None,
        "custom_extensions": ""
    }
    for i in range(1, len(parts) + 1):
        ancestor = '/'.join(parts[:i])
        if ancestor in config:
            c = config[ancestor]
            for key in effective:
                if key in c and c[key] is not None:
                    effective[key] = c[key]
    return effective

def apply_filters_and_cleanup(config):
    """After sync, delete files that don't match filter OR are older than delete_after."""
    files_processed = 0
    files_deleted = 0

    for root, dirs, files in os.walk(SHARE_DIR):
        for filename in files:
            filepath = os.path.join(root, filename)
            rel_folder = os.path.relpath(root, SHARE_DIR)

            effective = get_effective_config(rel_folder, config)

            if not effective["sync"]:
                continue

            ext = os.path.splitext(filename)[1].lower()
            files_processed += 1

            filter_type = effective["filter"]
            if filter_type == "custom":
                allowed = [f".{e.strip().lstrip('.')}" for e in effective["custom_extensions"].split(',') if e.strip()]
            else:
                allowed = FILTER_EXTENSIONS.get(filter_type)

            if allowed is not None and ext not in allowed:
                log(f"[FILTER] Removing {filepath} (ext {ext} not in {allowed})")
                os.remove(filepath)
                files_deleted += 1
                continue

            delete_after = effective["delete_after"]
            days = DELETE_DAYS.get(delete_after)
            if days is not None:
                file_age_days = (time.time() - os.path.getmtime(filepath)) / 86400
                if file_age_days > days:
                    log(f"[AGE] Removing {filepath} (age {file_age_days:.0f}d > {days}d)")
                    os.remove(filepath)
                    files_deleted += 1

    return files_processed, files_deleted

def main():
    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting sync...")

    if not acquire_lock():
        return

    try:
        config = load_config()
        status = load_status()
        errors = []

        need_resync = False
        try:
            log("[folders] Ermittle Ordnerstruktur per Graph API...")
            all_folders = list_all_folders()
            log(f"[folders] {len(all_folders)} Ordner gefunden")
            need_resync = write_sync_list(config, all_folders)
        except Exception as e:
            log(f"[WARN] Konnte sync_list nicht aktualisieren: {e}")
            errors.append(f"{datetime.now().strftime('%H:%M')} sync_list Fehler: {e}")

        ok, err = run_onedrive_sync(need_resync)
        if not ok:
            errors.append(f"{datetime.now().strftime('%H:%M')} Sync error: {err}")

        files_processed, files_deleted = apply_filters_and_cleanup(config)
        log(f"[SYNC] Processed {files_processed} files, deleted {files_deleted} locally")

        status["last_sync"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        status["files_synced"] = files_processed - files_deleted
        status["authenticated"] = True
        if errors:
            status["errors"] = (status.get("errors", []) + errors)[-10:]
        save_status(status)
        log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sync complete.")
    finally:
        release_lock()

if __name__ == '__main__':
    main()
