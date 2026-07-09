#!/usr/bin/env python3
"""OneDrive SyncServer - Sync Manager

Fuehrt den eigentlichen Sync durch:
0. Prueft eine Lock-Datei um zu verhindern dass zwei Sync-Laeufe
   gleichzeitig laufen.
1. Ermittelt Ordner per Graph API und schreibt eine sync_list Datei.
   WICHTIG (Design-Entscheidung nach mehreren fehlgeschlagenen Versuchen
   mit Exclude-Regeln): sync_list schliesst PER DEFAULT alles aus, was
   nicht explizit gelistet ist. Deshalb werden HIER NUR die AKTIVIERTEN
   Ordner als Include-Zeilen geschrieben (root-verankert mit fuehrendem
   "/", je zwei Zeilen pro Ordner: der reine Name fuer den Ordner-Eintrag
   selbst, und "Name/*" fuer den Inhalt). Es werden KEINE Exclude-Zeilen
   (!...) mehr verwendet - das vermeidet zwei beobachtete Probleme:
   a) Ordner-Reihenfolge/"Exclusions come first"-Anforderung in v2.5.x
   b) Ordner ohne fuehrenden "/" werden als teure "anywhere"-Regeln
      behandelt und koennen bei tiefen/grossen Baeumen (z.B. alte Build-
      Verzeichnisse mit tausenden Unterordnern) zu Haengern fuehren.
   Steigt NICHT in deaktivierte Aeste ohne Unterordner-Overrides hinab
   (spart API-Calls). Nutzt requests.Session() fuer Connection-Pooling.
   Wenn sich die sync_list geaendert hat, wird automatisch --resync
   mitgegeben.
2. onedrive --sync --bidirectional-sync --syncdir /share/onedrive.
   WICHTIG: Frueher liefen wir mit --download-only (reines Backup, nie
   hochladen). Der Nutzer will jedoch auch lokal abgelegte/geaenderte
   Dateien nach OneDrive hochladen koennen - deshalb jetzt echter
   bidirektionaler Sync (Standardverhalten von 'onedrive --sync' ohne
   --download-only/--upload-only): lokale Aenderungen werden hochgeladen,
   OneDrive-Aenderungen heruntergeladen, Loeschungen propagieren in beide
   Richtungen (Vorsicht: auch lokale Loeschungen loeschen jetzt online!).
   Falls onedrive trotzdem zur Laufzeit einen Resync verlangt, wird
   automatisch EINMAL mit --resync --resync-auth nachversucht.
3. Filtert Dateien innerhalb synchronisierter Ordner nach Dateityp/Alter
   (nur fuer lokale Aufraeumung basierend auf Alter/Typ - loescht NICHT
   online, sondern nur die lokale Kopie; die Datei bleibt in OneDrive).
4. Schreibt Status in sync_status.json
"""

import hashlib
import json
import os
import subprocess
import time
import urllib.parse
from datetime import datetime

import requests

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

MAX_FOLDER_DEPTH = 10
LOCK_STALE_SECONDS = 900

FILTER_EXTENSIONS = {
    "all": None,
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
    komplett wenn stdout kein Terminal ist (z.B. docker logs)."""
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


# --- Graph API Hilfsfunktionen (requests.Session fuer Connection-Reuse) ---

def get_access_token(session):
    rt_file = f"{ONEDRIVE_CONFIG_DIR}/refresh_token"
    if not os.path.exists(rt_file):
        raise Exception("Nicht authentifiziert")
    with open(rt_file) as f:
        refresh_token = f.read().strip()
    resp = session.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "scope": SCOPE,
    }, timeout=30)
    result = resp.json()
    if resp.status_code != 200:
        raise Exception(f"Token-Fehler: {result.get('error_description', result.get('error'))}")
    if "refresh_token" in result:
        with open(rt_file, 'w') as f:
            f.write(result["refresh_token"])
    return result["access_token"]


def resolve_enabled(path, config):
    parts = path.split("/")
    enabled = True
    for i in range(1, len(parts) + 1):
        ancestor = "/".join(parts[:i])
        if ancestor in config and "sync" in config[ancestor]:
            enabled = config[ancestor]["sync"]
    return enabled


def has_descendant_overrides(path, config):
    """Prueft ob IRGENDEIN Konfigurations-Eintrag fuer einen Unterordner
    von 'path' existiert. Wenn nicht, kann die Rekursion in diesen Ast
    sicher uebersprungen werden falls der Ast selbst deaktiviert ist."""
    prefix = path + "/"
    return any(key.startswith(prefix) for key in config)


def list_all_folders(config):
    """
    Listet Ordner per Graph API (Pfad-Format wie lokal: 'Top/Sub/Sub2').
    Nutzt eine requests.Session fuer Connection-Pooling. Steigt NICHT in
    Aeste hinab, die deaktiviert sind und keine Unterordner-Overrides in
    der Konfiguration haben.
    """
    session = requests.Session()
    token = get_access_token(session)
    session.headers.update({"Authorization": f"Bearer {token}"})

    folders = []
    skipped_branches = {"n": 0}
    counter = {"n": 0}

    def walk(item_id, path, depth):
        if depth > MAX_FOLDER_DEPTH:
            return
        resp = session.get(
            f"{GRAPH_BASE}/me/drive/items/{item_id}/children",
            params={"$select": "id,name,folder"}, timeout=30
        )
        resp.raise_for_status()
        result = resp.json()
        for item in result.get("value", []):
            if "folder" in item:
                child_path = f"{path}/{item['name']}" if path else item['name']
                folders.append(child_path)
                counter["n"] += 1
                if counter["n"] % 25 == 0:
                    log(f"[folders] ... {counter['n']} Ordner bisher gefunden")

                if item["folder"].get("childCount", 0) == 0:
                    continue

                enabled = resolve_enabled(child_path, config)
                if not enabled and not has_descendant_overrides(child_path, config):
                    skipped_branches["n"] += 1
                    continue

                walk(item["id"], child_path, depth + 1)

    walk("root", "", 0)
    session.close()
    if skipped_branches["n"]:
        log(f"[folders] {skipped_branches['n']} deaktivierte Aeste ohne Overrides uebersprungen (nicht weiter erkundet)")
    return sorted(folders)


def write_sync_list(config, all_folders):
    """
    Schreibt eine onedrive sync_list Datei - NUR mit Include-Zeilen fuer
    aktivierte Ordner (root-verankert). sync_list schliesst per Default
    alles aus, das nicht gelistet ist, daher sind keine Exclude-Zeilen
    noetig. Gibt zurueck ob sich der Inhalt gegenueber dem letzten Lauf
    geaendert hat (per Hash-Vergleich) - das entscheidet ob --resync
    noetig ist.
    """
    changed = False

    if not all_folders:
        return changed

    top_level_folders = [f for f in all_folders if "/" not in f]
    includes = []
    for path in all_folders:
        if resolve_enabled(path, config):
            includes.append(f"/{path}")
            includes.append(f"/{path}/*")

    if len(includes) == len(top_level_folders) * 2 and all(
        resolve_enabled(f, config) for f in all_folders
    ):
        # Alles was gefunden wurde ist aktiv UND es gibt keine deaktivierten
        # Top-Ordner, die wir wegen has_descendant_overrides uebersprungen
        # haben koennten -> keine Einschraenkung noetig (voller Sync).
        new_content = ""
    else:
        new_content = "\n".join(includes) + "\n" if includes else "/__NICHTS_AKTIVIERT__\n"

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
        log(f"[sync_list] {len(includes)} Einschluss-Zeilen geschrieben (nur Includes, root-verankert)")

    if changed:
        log("[sync_list] Aenderung erkannt - dieser Sync laeuft mit --resync")

    return changed


def run_onedrive_sync(need_resync):
    """
    Fuehrt den eigentlichen onedrive Sync durch - echter bidirektionaler
    Sync (kein --download-only mehr): laedt lokale Aenderungen hoch UND
    OneDrive-Aenderungen runter.
    --syncdir /share/onedrive: WICHTIG - ohne dieses Flag laedt onedrive
    standardmaessig nach ~/OneDrive im Container-Dateisystem statt in den
    persistenten HA-Share-Mount.
    --sync: fuehrt den eigentlichen Abgleich durch (Pflicht-Flag).

    ROBUSTHEIT: Falls der erste Versuch trotzdem mit "resync is required"
    fehlschlaegt, wird automatisch EINMAL mit --resync --resync-auth
    nachversucht.
    """
    def build_cmd(with_resync):
        cmd = [
            "onedrive",
            "--confdir", ONEDRIVE_CONFIG_DIR,
            "--syncdir", SHARE_DIR,
            "--sync",
            "--verbose"
        ]
        if with_resync:
            cmd += ["--resync", "--resync-auth"]
        return cmd

    def run_once(with_resync):
        cmd = build_cmd(with_resync)
        log(f"[onedrive] Starte: {' '.join(cmd)}")
        proc = None
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
            full_output = "".join(output_lines)
            if proc.returncode != 0:
                err_msg = "".join(output_lines[-30:]).strip() or f"Exit-Code {proc.returncode} ohne Ausgabe"
                return False, err_msg, full_output
            return True, None, full_output
        except subprocess.TimeoutExpired:
            if proc:
                proc.kill()
            return False, "Sync timeout after 600s", ""
        except Exception as e:
            return False, str(e), ""

    ok, err, output = run_once(need_resync)
    if not ok and not need_resync and ("resync is required" in output.lower() or "sync_dir" in output.lower()):
        log("[onedrive] Automatischer Nachversuch mit --resync (onedrive verlangte es zur Laufzeit)...")
        ok, err, output = run_once(True)
    if not ok:
        log(f"[WARN] onedrive Sync fehlgeschlagen: {err}")
        return False, err
    return True, None


def get_effective_config(folder_path, config):
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
    """
    Loescht NUR lokale Kopien (nie online) von Dateien die nicht zum
    Filter passen oder zu alt sind.
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
            t0 = time.time()
            all_folders = list_all_folders(config)
            log(f"[folders] {len(all_folders)} Ordner gefunden in {time.time()-t0:.1f}s")
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
