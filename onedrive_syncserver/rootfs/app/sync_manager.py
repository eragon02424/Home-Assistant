#!/usr/bin/env python3
"""OneDrive SyncServer - Sync Manager

Fuehrt den eigentlichen Sync durch:
0. Prueft eine Lock-Datei um zu verhindern dass zwei Sync-Laeufe
   gleichzeitig laufen.
1. Ermittelt Ordner per Graph API und schreibt eine sync_list Datei -
   NUR Include-Zeilen fuer aktivierte Ordner (root-verankert, "/Pfad" und
   "/Pfad/*" pro Ordner, praezise pro Unterordner-Ebene). Steigt NICHT in
   deaktivierte Aeste ohne Unterordner-Overrides hinab. Nutzt
   requests.Session() fuer Connection-Pooling. Wenn sich die sync_list
   geaendert hat, wird automatisch --resync mitgegeben.
2. onedrive --sync --download-only --syncdir /share/onedrive.

   *** SICHERHEITSKRITISCH - NIEMALS AENDERN OHNE AUSDRUECKLICHE
   BESTAETIGUNG DES NUTZERS ***
   Zwischenzeitlich lief dieses Add-on OHNE --download-only (echter
   bidirektionaler Sync). Das fuehrte zu einem ECHTEN DATENVERLUST-
   VORFALL am 09.07.2026: unser eigener lokaler Cleanup-Schritt (Schritt
   3, loescht lokale Kopien deaktivierter Ordner) wurde von onedrive als
   "Nutzer hat das online geloescht" interpretiert und die Loeschung
   NACH OneDrive hochgeladen - 13 Scan-Ordner wurden dadurch tatsaechlich
   von OneDrive selbst geloescht (nur durch den OneDrive-Papierkorb
   rettbar gewesen). --download-only verhindert das strukturell: es
   werden NIEMALS lokale Aenderungen (auch keine Loeschungen) nach
   OneDrive hochgeladen, unabhaengig davon was unser eigener Cleanup-
   Code lokal tut. Echter Upload-Support (neue lokale Dateien zu OneDrive
   hochladen) ist ein separates, bewusst zu entwerfendes Feature fuer
   die Zukunft - NICHT einfach durch Entfernen von --download-only
   nachruestbar, da genau das den Vorfall verursacht hat.

   AKTIVITAETS-UEBERWACHUNG statt festem Zeit-Limit: Jede neue Zeile
   Ausgabe zaehlt als Lebenszeichen. Nur wenn STALL_TIMEOUT_SECONDS lang
   GAR NICHTS mehr kommt, wird der Prozess als haengen geblieben
   betrachtet und abgebrochen. Ansonsten darf der Sync beliebig lange
   laufen (auch mehrere Stunden bei sehr grossen Strukturen).
   Der aktuelle Fortschritt wird laufend in PROGRESS_FILE geschrieben.
3. Filtert Dateien innerhalb synchronisierter Ordner nach Dateityp/Alter,
   und entfernt komplette lokale Kopien deaktivierter Ordner (Altlasten).
   Alles NUR lokal - onedrive laeuft im --download-only Modus und laedt
   diese Loeschungen NIE nach OneDrive hoch.
4. Schreibt Status in sync_status.json
"""

import hashlib
import json
import os
import select
import shutil
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
PROGRESS_FILE = f"{CONFIG_DIR}/sync_progress.json"

CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "Files.ReadWrite Files.ReadWrite.All Sites.ReadWrite.All offline_access"

MAX_FOLDER_DEPTH = 10
LOCK_STALE_SECONDS = 900
STALL_TIMEOUT_SECONDS = 300  # 5 Minuten OHNE jede neue Ausgabe = echter Haenger

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

def write_progress(phase, detail, active=True):
    """Schreibt den aktuellen Fortschritt fuer die Web-UI (live Polling)."""
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({
                "active": active,
                "phase": phase,
                "detail": detail,
                "updated_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                "updated_ts": time.time()
            }, f)
    except Exception:
        pass

def clear_progress():
    write_progress("idle", "", active=False)


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
                    write_progress("folders", f"{counter['n']} Ordner gefunden, zuletzt: {child_path}")

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
    alles aus, das nicht gelistet ist.

    WICHTIG (Unterordner-Granularitaet): Fuer jeden Ordner wird NUR dann
    "/Pfad/*" (rekursiver Inhalt) geschrieben, wenn AUCH ALLE seine
    bekannten Unterordner aktiviert sind. Hat ein Ordner deaktivierte
    Unterordner, wird NUR "/Pfad" (der Ordner-Eintrag selbst, KEIN "/*")
    geschrieben, und stattdessen fuer jeden aktivierten Unterordner
    einzeln "/Pfad/Unterordner" + ggf. "/Pfad/Unterordner/*".
    """
    changed = False

    if not all_folders:
        return changed

    children_of = {}
    for path in all_folders:
        parent = "/".join(path.split("/")[:-1])
        children_of.setdefault(parent, []).append(path)

    includes = []

    def has_disabled_descendant(path):
        if not resolve_enabled(path, config):
            return True
        for child in children_of.get(path, []):
            if has_disabled_descendant(child):
                return True
        return False

    def emit(path):
        if not resolve_enabled(path, config):
            return
        includes.append(f"/{path}")
        if has_disabled_descendant(path):
            for child in children_of.get(path, []):
                emit(child)
        else:
            includes.append(f"/{path}/*")

    top_level = children_of.get("", [])
    for path in top_level:
        emit(path)

    if all(resolve_enabled(f, config) for f in all_folders):
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
        log(f"[sync_list] {len(includes)} Einschluss-Zeilen geschrieben (praezise pro Unterordner-Ebene)")

    if changed:
        log("[sync_list] Aenderung erkannt - dieser Sync laeuft mit --resync")

    return changed


def run_onedrive_sync(need_resync):
    """
    Fuehrt den eigentlichen onedrive Sync durch.

    *** --download-only ist SICHERHEITSKRITISCH, siehe Modul-Docstring. ***
    Laedt NUR von OneDrive runter. Lokale Aenderungen (inkl. Loeschungen
    durch unseren eigenen Cleanup-Schritt) werden NIEMALS nach OneDrive
    hochgeladen.

    AKTIVITAETS-UEBERWACHUNG statt festem Zeit-Limit: Jede neue Zeile
    Ausgabe zaehlt als Lebenszeichen. Nur wenn STALL_TIMEOUT_SECONDS lang
    GAR NICHTS mehr kommt, wird der Prozess als haengen geblieben
    betrachtet und abgebrochen.
    """
    def build_cmd(with_resync):
        cmd = [
            "onedrive",
            "--confdir", ONEDRIVE_CONFIG_DIR,
            "--syncdir", SHARE_DIR,
            "--sync",
            "--download-only",
            "--verbose"
        ]
        if with_resync:
            cmd += ["--resync", "--resync-auth"]
        return cmd

    def run_once(with_resync):
        cmd = build_cmd(with_resync)
        log(f"[onedrive] Starte: {' '.join(cmd)}")
        write_progress("sync", "onedrive Prozess gestartet...")
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1
            )
            output_lines = []
            last_activity = time.time()

            while True:
                if proc.poll() is not None:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            log(line)
                            output_lines.append(line)
                            write_progress("sync", line)
                    break

                ready, _, _ = select.select([proc.stdout], [], [], 5.0)
                if ready:
                    line = proc.stdout.readline()
                    if line:
                        line_s = line.rstrip()
                        log(line_s)
                        output_lines.append(line_s)
                        write_progress("sync", line_s)
                        last_activity = time.time()
                else:
                    stalled_for = time.time() - last_activity
                    if stalled_for > STALL_TIMEOUT_SECONDS:
                        log(f"[WARN] Keine Aktivitaet seit {stalled_for:.0f}s - breche ab (echter Haenger)")
                        proc.kill()
                        proc.wait(timeout=10)
                        return False, f"Stalled - {STALL_TIMEOUT_SECONDS}s keine Aktivitaet", "\n".join(output_lines)

            proc.wait()
            full_output = "\n".join(output_lines)
            if proc.returncode != 0:
                err_msg = "\n".join(output_lines[-30:]).strip() or f"Exit-Code {proc.returncode} ohne Ausgabe"
                return False, err_msg, full_output
            return True, None, full_output
        except Exception as e:
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
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
    Loescht NUR lokale Kopien (NIEMALS online - onedrive laeuft im
    --download-only Modus, die Loeschungen hier werden nicht hochgeladen):
    - Ordner die per Konfiguration DEAKTIVIERT sind: KOMPLETT loeschen
      (Altlasten von frueheren, ungefilterten Sync-Laeufen).
    - Dateien die nicht zum Filter passen oder zu alt sind: einzeln
      loeschen.
    """
    files_processed = 0
    files_deleted = 0

    # Schritt 1: komplette deaktivierte Ordner loeschen
    for root, dirs, files in list(os.walk(SHARE_DIR)):
        rel_folder = os.path.relpath(root, SHARE_DIR)
        if rel_folder == ".":
            continue
        effective = get_effective_config(rel_folder, config)
        if not effective["sync"]:
            try:
                file_count = sum(len(fs) for _, _, fs in os.walk(root))
                log(f"[CLEANUP] Entferne deaktivierten Ordner komplett (nur lokal): {root} ({file_count} Dateien)")
                shutil.rmtree(root)
                files_deleted += file_count
            except FileNotFoundError:
                pass
            dirs[:] = []

    # Schritt 2: Dateityp-/Alters-Filter innerhalb aktivierter Ordner
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
                log(f"[FILTER] Removing {filepath} (nur lokal, ext {ext} not in {allowed})")
                os.remove(filepath)
                files_deleted += 1
                continue

            delete_after = effective["delete_after"]
            days = DELETE_DAYS.get(delete_after)
            if days is not None:
                file_age_days = (time.time() - os.path.getmtime(filepath)) / 86400
                if file_age_days > days:
                    log(f"[AGE] Removing {filepath} (nur lokal, age {file_age_days:.0f}d > {days}d)")
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
            write_progress("folders", "Ermittle Ordnerstruktur per Graph API...")
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

        write_progress("cleanup", "Filtere und raeume lokale Dateien auf...")
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
        clear_progress()
        release_lock()

if __name__ == '__main__':
    main()
