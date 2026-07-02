#!/usr/bin/env python3
"""
Prüft ob externe Abhängigkeiten in custom HA Add-ons veraltet sind.
Gibt JSON aus das von der HA Automation weiterverarbeitet wird.
"""

import urllib.request
import urllib.error
import json
import re
import sys

def get_github_latest_release(owner, repo):
    """Holt das neueste Release-Tag von GitHub."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "HA-Addon-Checker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("tag_name", "").lstrip("v")
    except Exception as e:
        return None

def get_github_file(owner, repo, path):
    """Liest eine Datei direkt aus GitHub (raw)."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "HA-Addon-Checker/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        return None

def extract_version_from_config(content):
    """Extrahiert die version aus einer config.yaml."""
    if not content:
        return None
    match = re.search(r'^version:\s*["\']?([\d\.]+)["\']?', content, re.MULTILINE)
    return match.group(1) if match else None

def extract_github_mcp_version_from_dockerfile(content):
    """
    Der mcp_github Dockerfile lädt immer die 'latest' Version beim Build.
    Die tatsächlich verwendete Version steht nicht explizit drin.
    Wir prüfen ob die config.yaml Version (unser Add-on) == neueste github-mcp-server Version.
    """
    # mcp_github config.yaml version ist unsere eigene Versionsnummer,
    # nicht die des github-mcp-server Binaries.
    # Daher vergleichen wir separat.
    return None

# Liste der zu prüfenden Add-ons
# Format: (addon_name, addon_config_path, upstream_github_owner, upstream_github_repo)
ADDONS_TO_CHECK = [
    {
        "name": "MCP GitHub (github-mcp-server Binary)",
        "addon_slug": "mcp_github",
        "upstream_owner": "github",
        "upstream_repo": "github-mcp-server",
        "version_key": "github_mcp_server_version",
        # gespeicherte Version wird in /config/addon_update_versions.json abgelegt
    },
    {
        "name": "Grocy Barcode Buddy",
        "addon_slug": "grocy_barcode_buddy",
        "upstream_owner": "Forceu",
        "upstream_repo": "barcodebuddy",
        "version_key": "barcode_buddy_version",
    },
]

def load_tracked_versions(filepath):
    """Lädt gespeicherte Versionen aus JSON Datei."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_tracked_versions(filepath, data):
    """Speichert Versionen in JSON Datei."""
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)

def main():
    versions_file = "/config/addon_update_versions.json"
    tracked = load_tracked_versions(versions_file)
    
    updates_needed = []
    
    for addon in ADDONS_TO_CHECK:
        name = addon["name"]
        upstream_owner = addon["upstream_owner"]
        upstream_repo = addon["upstream_repo"]
        version_key = addon["version_key"]
        
        # Neueste Version von GitHub holen
        latest = get_github_latest_release(upstream_owner, upstream_repo)
        
        if not latest:
            print(f"WARN: Konnte Version für {name} nicht abrufen", file=sys.stderr)
            continue
        
        # Zuletzt beim Build verwendete Version
        installed = tracked.get(version_key, "unbekannt")
        
        if installed == "unbekannt":
            # Erste Ausführung - aktuelle Version als Baseline speichern
            tracked[version_key] = latest
            print(f"INFO: {name} - Baseline gesetzt auf {latest}", file=sys.stderr)
        elif installed != latest:
            updates_needed.append({
                "name": name,
                "installed": installed,
                "latest": latest,
                "version_key": version_key
            })
            print(f"UPDATE: {name} - installiert: {installed}, verfügbar: {latest}", file=sys.stderr)
        else:
            print(f"OK: {name} - {latest} ist aktuell", file=sys.stderr)
    
    save_tracked_versions(versions_file, tracked)
    
    # Ausgabe als JSON für HA Automation
    result = {
        "updates_available": len(updates_needed) > 0,
        "count": len(updates_needed),
        "addons": updates_needed
    }
    print(json.dumps(result))

if __name__ == "__main__":
    main()
