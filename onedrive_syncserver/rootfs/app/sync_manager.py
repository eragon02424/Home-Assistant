#!/usr/bin/env python3
"""OneDrive SyncServer - Sync Manager

Fuehrt den eigentlichen Sync durch:
1. Ermittelt ALLE Ordner (rekursiv) via Graph API und schreibt eine
   sync_list Datei mit Include/Exclude-Zeilen an den "Grenzen" zwischen
   aktivierten und deaktivierten Aesten - so werden abgewaehlte Ordner
   (auch verschachtelt) GAR NICHT erst heruntergeladen.
2. onedrive --synchronize (OneDrive ist Master, no-remote-delete fuer
   lokale Loeschungen)
3. Filtert Dateien innerhalb synchronisierter Ordner nach Dateityp/Alter
4. Schreibt Status in sync_status.json
"""

import json
import os
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

CONFIG_DIR = "/data"
SYNC_CONFIG = f"{CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR = f"{CONFIG_DIR}/onedrive"
SYNC_LIST_FILE = f"{ONEDRIVE_CONFIG_DIR}/sync_list"
SHARE_DIR = "/share/onedrive"
STATUS_FILE = f"{CONFIG_DIR}/sync_status.json"

CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "Files.ReadWrite Files.ReadWrite.All Sites.ReadWrite.All offline_access"

MAX_FOLDER_DEPTH = 10  # Sicherheitsgrenze gegen extrem tiefe Baeume

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

    WICHTIG: onedrive's sync_list ist eine reine POSITIVLISTE - sobald die
    Datei existiert, wird NUR synchronisiert was explizit als Include-Zeile
    drinsteht. Deshalb bekommt JEDER aktivierte Top-Level-Ordner immer eine
    explizite Include-Zeile (Root-Ebene wird bewusst als "nicht inklusiv"
    behandelt, anders als tiefere Ebenen die vom Elternordner erben) -
    andernfalls wuerden default-aktive Top-Ordner beim Schreiben der Datei
    versehentlich mit ausgeschlossen, sobald IRGENDWO im Baum eine
    Abweichung vorliegt.
    Tiefere Ebenen bekommen nur an den tatsaechlichen Uebergaengen
    (aktiviert->deaktiviert bzw. umgekehrt) eine Zeile - das erlaubt auch
    verschachteltes Ein-/Ausschalten (z.B. Dokumente an, Dokumente/Anno1404
    aus, Dokumente/Anno1800 und Anno2205 an).
    Wenn nirgendwo eine Abweichung vom Standard (alles an) vorliegt, wird
    keine sync_list geschrieben - dann laeuft der volle Sync wie gewohnt.
    """
    if not all_folders:
        # Ordnerliste konnte nicht ermittelt werden - keine Einschraenkung
        # setzen, um nichts kaputt zu machen.
        return

    top_level_folders = [f for f in all_folders if "/" not in f]
    includes = []
    excludes = []
    for path in all_folders:
        cur_enabled = resolve_enabled(path, config)
        parts = path.split("/")
        if len(parts) == 1:
            # Root-Ebene: IMMER als "nicht inklusiv" behandeln, damit jeder
            # aktivierte Top-Ordner eine eigene Include-Zeile bekommt.
            parent_enabled = False
        else:
            parent_enabled = resolve_enabled("/".join(parts[:-1]), config)
        if cur_enabled and not parent_enabled:
            includes.append(f"{path}/*")
        elif not cur_enabled and parent_enabled:
            excludes.append(f"!{path}/*")

    # Wenn keine Ausschluesse existieren UND jeder Top-Ordner enthalten ist,
    # ist alles Standard (an) - keine Einschraenkung noetig.
    if not excludes and len(includes) == len(top_level_folders):
        if os.path.exists(SYNC_LIST_FILE):
            os.remove(SYNC_LIST_FILE)
        print("[sync_list] Alle Ordner aktiv - keine Einschraenkung")
        return

    with open(SYNC_LIST_FILE, "w") as f:
        for line in includes:
            f.write(line + "\n")
        for line in excludes:
            f.write(line + "\n")
    print(f"[sync_list] {len(includes)} Einschluss-, {len(excludes)} Ausschluss-Regeln geschrieben")


def run_onedrive_sync():
    """Run onedrive sync with OneDrive as master.
    --no-remote-delete: local deletes don't propagate to OneDrive
    OneDrive deletions DO propagate locally (master behavior)
    sync_list (falls vorhanden) beschraenkt was ueberhaupt geladen wird.
    """
    try:
        result = subprocess.run(
            [
                "onedrive",
                "--confdir", ONEDRIVE_CONFIG_DIR,
                "--synchronize",
                "--no-remote-delete",  # local deletes stay local
                "--verbose"
            ],
            capture_output=True, text=True, timeout=300
        )
        print(result.stdout)
        if result.returncode != 0:
            print(f"[WARN] onedrive exited with {result.returncode}: {result.stderr}")
            return False, result.stderr
        return True, None
    except subprocess.TimeoutExpired:
        return False, "Sync timeout after 300s"
    except Exception as e:
        return False, str(e)

def get_effective_config(folder_path, config):
    """Resolves inherited config for a folder.
    Child inherits from parent if value is 'inherit' or not set.
    """
    parts = folder_path.split('/')
    effective = {
        "sync": True,
        "filter": "all",
        "delete_after": "never",
        "custom_local_path": None,
        "custom_extensions": ""
    }
    # Walk from root to leaf, inheriting values
    for i in range(1, len(parts) + 1):
        ancestor = '/'.join(parts[:i])
        if ancestor in config:
            c = config[ancestor]
            for key in effective:
                if key in c and c[key] is not None:
                    effective[key] = c[key]
    return effective

def apply_filters_and_cleanup(config):
    """After sync, delete files that don't match filter OR are older than delete_after.
    Only applies to folders that WERE synced (sync_list already excluded the rest
    from being downloaded in the first place). Never touches OneDrive.
    """
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

            # --- Filter check ---
            filter_type = effective["filter"]
            if filter_type == "custom":
                allowed = [f".{e.strip().lstrip('.')}" for e in effective["custom_extensions"].split(',') if e.strip()]
            else:
                allowed = FILTER_EXTENSIONS.get(filter_type)  # None = all allowed

            if allowed is not None and ext not in allowed:
                # File doesn't match filter - delete locally, keep on OneDrive
                print(f"[FILTER] Removing {filepath} (ext {ext} not in {allowed})")
                os.remove(filepath)
                files_deleted += 1
                continue

            # --- Age check ---
            delete_after = effective["delete_after"]
            days = DELETE_DAYS.get(delete_after)
            if days is not None:
                file_age_days = (time.time() - os.path.getmtime(filepath)) / 86400
                if file_age_days > days:
                    print(f"[AGE] Removing {filepath} (age {file_age_days:.0f}d > {days}d)")
                    os.remove(filepath)
                    files_deleted += 1

    return files_processed, files_deleted

def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting sync...")
    config = load_config()
    status = load_status()
    errors = []

    # 0. sync_list VOR dem Sync setzen (rekursiv, alle Ebenen), damit
    #    abgewaehlte Ordner - auch verschachtelt - gar nicht erst
    #    heruntergeladen werden.
    try:
        all_folders = list_all_folders()
        write_sync_list(config, all_folders)
    except Exception as e:
        print(f"[WARN] Konnte sync_list nicht aktualisieren: {e}")
        errors.append(f"{datetime.now().strftime('%H:%M')} sync_list Fehler: {e}")

    # 1. Run onedrive sync
    ok, err = run_onedrive_sync()
    if not ok:
        errors.append(f"{datetime.now().strftime('%H:%M')} Sync error: {err}")

    # 2. Apply filters and age-based cleanup (nur innerhalb bereits
    #    synchronisierter Ordner - der grosse Teil der Nicht-Ordner ist
    #    dank sync_list bereits gar nicht heruntergeladen worden)
    files_processed, files_deleted = apply_filters_and_cleanup(config)
    print(f"[SYNC] Processed {files_processed} files, deleted {files_deleted} locally")

    # 3. Update status
    status["last_sync"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    status["files_synced"] = files_processed - files_deleted
    status["authenticated"] = True
    # Keep last 10 errors
    if errors:
        status["errors"] = (status.get("errors", []) + errors)[-10:]
    save_status(status)
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Sync complete.")

if __name__ == '__main__':
    main()
