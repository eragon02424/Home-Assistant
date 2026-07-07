#!/usr/bin/env python3
"""OneDrive SyncServer - Sync Manager

Führt den eigentlichen Sync durch:
1. onedrive --synchronize (OneDrive ist Master, no-remote-delete für lokale Löschungen)
2. Filtert Dateien basierend auf Konfiguration
3. Löscht lokale Dateien die älter als X Tage (ohne OneDrive zu berühren)
4. Schreibt Status in sync_status.json
"""

import json
import os
import subprocess
import time
from datetime import datetime

CONFIG_DIR = "/data"
SYNC_CONFIG = f"{CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR = f"{CONFIG_DIR}/onedrive"
SHARE_DIR = "/share/onedrive"
STATUS_FILE = f"{CONFIG_DIR}/sync_status.json"

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

def run_onedrive_sync():
    """Run onedrive sync with OneDrive as master.
    --no-remote-delete: local deletes don't propagate to OneDrive
    OneDrive deletions DO propagate locally (master behavior)
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

def apply_filters_and_cleanup(config):
    """After sync, delete files that don't match filter OR are older than delete_after.
    Never touches OneDrive - only local /share/onedrive.
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

    # 1. Run onedrive sync
    ok, err = run_onedrive_sync()
    if not ok:
        errors.append(f"{datetime.now().strftime('%H:%M')} Sync error: {err}")

    # 2. Apply filters and age-based cleanup
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
