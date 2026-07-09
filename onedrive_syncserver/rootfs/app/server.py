#!/usr/bin/env python3
"""OneDrive SyncServer - Web UI Backend
Port 8772 = Ingress (BASE aus X-Ingress-Path Header)

WICHTIG: Device Code Flow (/devicecode Endpoint) funktioniert NICHT fuer
private Microsoft-Konten (MSA wie @live.com/@outlook.com/@hotmail.com) -
Microsoft blockiert diesen Flow serverseitig fuer nicht explizit von MS
freigeschaltete Apps.
Deshalb nutzen wir stattdessen die interaktive Browser-URL-Methode
(--auth-files des onedrive CLI-Clients), die fuer MSA-Konten funktioniert.

WICHTIG 2: Der onedrive CLI Prozess der die authUrl erzeugt hat muss am
Leben bleiben bis der Code-Austausch passiert ist.

WICHTIG 3: Die Ordner-Konfiguration laedt Ordner LAZY per Graph API - nur
die aktuell sichtbare Ebene wird geladen, Unterordner erst beim Aufklappen
per Klick (/api/folder_children).

WICHTIG 4: trigger_sync() startet sync_manager.py in einem Thread OHNE
festes Zeit-Limit mehr (sync_manager.py ueberwacht selbst Aktivitaet ueber
STALL_TIMEOUT_SECONDS und darf bei grossen OneDrive-Strukturen mehrere
Stunden laufen, solange er aktiv arbeitet). Ein sehr grosszuegiger
Notfall-Deckel (12h) verhindert nur einen echten Endlos-Zombie-Prozess.

WICHTIG 5: /api/search nutzt Teilstring-Suche (nicht exakten Vergleich).

WICHTIG 6: /api/progress liefert den Live-Fortschritt des laufenden Syncs
(letzte Log-Zeile + Zeitstempel) aus sync_progress.json, das
sync_manager.py laufend aktualisiert - die UI pollt das waehrend eines
aktiven Syncs haeufiger als den normalen Status.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8772
IS_DIRECT_PORT = (PORT == 8771)

CONFIG_DIR = "/data"
SYNC_CONFIG = f"{CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR = f"{CONFIG_DIR}/onedrive"
AUTH_URL_FILE = f"{ONEDRIVE_CONFIG_DIR}/authUrl"
RESPONSE_URL_FILE = f"{ONEDRIVE_CONFIG_DIR}/responseUrl"
AUTH_DEBUG_LOG = f"{CONFIG_DIR}/auth_debug.log"
SHARE_DIR = "/share/onedrive"
DOWNLOAD_DIR = "/share/onedrive_downloads"
LOG_FILE = f"{CONFIG_DIR}/sync.log"
STATUS_FILE = f"{CONFIG_DIR}/sync_status.json"
PROGRESS_FILE = f"{CONFIG_DIR}/sync_progress.json"

CLIENT_ID = "d50ca740-c83f-4d1b-b616-12c519384f0c"
TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = "Files.ReadWrite Files.ReadWrite.All Sites.ReadWrite.All offline_access"

_auth_proc = None

def auth_log(msg):
    with open(AUTH_DEBUG_LOG, 'a') as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

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

def graph_get(access_token, path):
    url = f"{GRAPH_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        raise Exception(f"Graph {e.code}: {body.get('error', {}).get('message', str(e))}")

def graph_download(access_token, item_id, dest_path):
    url = f"{GRAPH_BASE}/me/drive/items/{item_id}/content"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        resp = urllib.request.urlopen(req, timeout=60)
        with open(dest_path, "wb") as f:
            f.write(resp.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read())
        raise Exception(f"Download {e.code}: {body.get('error', {}).get('message', str(e))}")

def load_sync_config():
    if os.path.exists(SYNC_CONFIG):
        with open(SYNC_CONFIG) as f:
            return json.load(f)
    return {}

def save_sync_config(config):
    with open(SYNC_CONFIG, "w") as f:
        json.dump(config, f, indent=2)

def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"last_sync": None, "files_synced": 0, "errors": []}

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"active": False, "phase": "idle", "detail": "", "updated_at": None}

def is_authenticated():
    return os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token")

def get_onedrive_folder_children(path=""):
    """
    Listet NUR die direkten Unterordner eines Pfades per Graph API
    (nicht rekursiv). path="" bedeutet Root. Gibt eine Liste von Dicts
    zurueck: {name, path, has_children}.
    """
    token = get_access_token()
    if path:
        encoded = urllib.parse.quote(path)
        result = graph_get(token, f"/me/drive/root:/{encoded}:/children?$select=id,name,folder")
    else:
        result = graph_get(token, "/me/drive/root/children?$select=id,name,folder")
    folders = []
    for item in result.get("value", []):
        if "folder" in item:
            child_path = f"{path}/{item['name']}" if path else item["name"]
            folders.append({
                "name": item["name"],
                "path": child_path,
                "has_children": item["folder"].get("childCount", 0) > 0
            })
    folders.sort(key=lambda x: x["name"].lower())
    return folders

def get_base():
    if IS_DIRECT_PORT:
        return ""
    return request.headers.get("X-Ingress-Path", "").rstrip("/")

FILTER_OPTIONS = [
    {"value": "all", "label": "Alle Dateien"},
    {"value": "pdf", "label": "Nur PDF"},
    {"value": "images", "label": "Nur Bilder (jpg, png, tiff, heic)"},
    {"value": "pdf_images", "label": "PDF + Bilder"},
    {"value": "office", "label": "Nur Office (docx, xlsx, pptx)"},
    {"value": "custom", "label": "Benutzerdefiniert"}
]

DELETE_OPTIONS = [
    {"value": "never", "label": "Nie"},
    {"value": "1d", "label": "Nach 1 Tag"},
    {"value": "7d", "label": "Nach 1 Woche"},
    {"value": "30d", "label": "Nach 1 Monat"},
    {"value": "180d", "label": "Nach 6 Monaten"},
    {"value": "365d", "label": "Nach 1 Jahr"}
]

HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate, max-age=0">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>OneDrive SyncServer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #111827; color: #f9fafb; min-height: 100vh; }
  .header { background: #1f2937; border-bottom: 1px solid #374151;
             padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.25rem; font-weight: 600; }
  .container { max-width: 1000px; margin: 0 auto; padding: 24px; }
  .card { background: #1f2937; border: 1px solid #374151; border-radius: 12px;
          padding: 20px; margin-bottom: 20px; }
  .card h2 { font-size: 1rem; font-weight: 600; color: #9ca3af;
             text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .card p.hint { color: #9ca3af; font-size: 0.85rem; margin-bottom: 16px; }
  .status-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .status-item { background: #111827; border-radius: 8px; padding: 12px; }
  .status-item .label { font-size: 0.75rem; color: #6b7280; margin-bottom: 4px; }
  .status-item .value { font-size: 1rem; font-weight: 600; }
  .ok { color: #10b981; } .error-c { color: #ef4444; }
  .progress-box { background: #111827; border: 1px solid #3b82f6; border-radius: 8px;
                  padding: 12px; margin-top: 12px; display: none; }
  .progress-box.show { display: block; }
  .progress-box .spinner { display: inline-block; width: 12px; height: 12px;
                            border: 2px solid #3b82f6; border-top-color: transparent;
                            border-radius: 50%; animation: spin 0.8s linear infinite;
                            margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .progress-box .label { font-size: 0.75rem; color: #6b7280; margin-bottom: 4px; }
  .progress-box .detail { font-size: 0.82rem; color: #e5e7eb; font-family: monospace;
                           word-break: break-all; }
  .auth-box { background: #111827; border: 1px solid #3b82f6; border-radius: 8px; padding: 16px; }
  .auth-box p { color: #9ca3af; margin-bottom: 12px; font-size: 0.9rem; }
  .auth-url-box { background: #1f2937; border: 1px solid #374151; border-radius: 6px;
                  padding: 10px; margin-bottom: 12px; word-break: break-all;
                  font-size: 0.8rem; color: #60a5fa; user-select: all; cursor: pointer; }
  .auth-debug-box { background: #111827; border: 1px solid #374151; border-radius: 6px;
                    padding: 10px; margin-top: 12px; font-family: monospace; font-size: 0.75rem;
                    color: #9ca3af; max-height: 150px; overflow-y: auto; white-space: pre-wrap; }
  .btn { padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
         font-size: 0.9rem; font-weight: 500; margin-right: 4px; }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn-primary { background: #3b82f6; color: white; }
  .btn-success { background: #10b981; color: white; }
  .auth-input { width: 100%; background: #1f2937; border: 1px solid #374151;
                color: #f9fafb; padding: 8px 12px; border-radius: 6px;
                font-size: 0.9rem; margin: 8px 0; }
  .folder-item { border-bottom: 1px solid #374151; }
  .folder-row { display: flex; align-items: center; gap: 8px; padding: 10px 8px; flex-wrap: wrap; }
  .folder-row:hover { background: #111827; border-radius: 6px; }
  .folder-indent { width: 20px; flex-shrink: 0; }
  .folder-expand { width: 20px; flex-shrink: 0; text-align: center; cursor: pointer;
                    color: #6b7280; font-size: 0.75rem; user-select: none; }
  .folder-expand.leaf { visibility: hidden; }
  .folder-name { flex: 1; font-size: 0.9rem; color: #e5e7eb; min-width: 150px; }
  .folder-name.disabled { color: #6b7280; }
  .folder-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  select, input[type=text] { background: #111827; border: 1px solid #374151; color: #f9fafb;
                               padding: 4px 8px; border-radius: 6px; font-size: 0.8rem; }
  input[type=text] { width: 200px; }
  .checkbox-label { display: flex; align-items: center; gap: 6px; cursor: pointer;
                    font-size: 0.85rem; color: #9ca3af; }
  .folder-children { display: none; }
  .folder-children.open { display: block; }
  .folder-loading { color: #6b7280; font-size: 0.8rem; padding: 6px 8px; }
  .log-box { background: #111827; border-radius: 8px; padding: 12px;
             font-family: monospace; font-size: 0.78rem; color: #9ca3af;
             max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
  .save-bar { position: sticky; bottom: 0; background: #1f2937;
              border-top: 1px solid #374151; padding: 16px 24px;
              display: flex; justify-content: flex-end; gap: 12px; }
  .toast { position: fixed; bottom: 80px; right: 24px; color: white;
           padding: 12px 20px; border-radius: 8px; font-size: 0.9rem;
           opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 100; }
  .toast.show { opacity: 1; }
  .error-item { color: #ef4444; font-size: 0.8rem; padding: 4px 0; }
  .dl-row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
  .dl-row input { flex: 1; min-width: 150px; width: auto; }
  .dl-result { margin-top: 10px; font-size: 0.85rem; }
  .dl-result.ok { color: #10b981; } .dl-result.err { color: #ef4444; }
  .search-results { margin-top: 12px; }
  .search-result-item { display: flex; justify-content: space-between; align-items: center;
                         padding: 8px 10px; background: #111827; border-radius: 6px;
                         margin-bottom: 6px; font-size: 0.85rem; }
  .search-result-item .path { color: #9ca3af; font-size: 0.78rem; margin-top: 2px; }
  .dl-btn { background: #3b82f6; color: white; border: none; padding: 4px 10px;
             border-radius: 4px; cursor: pointer; font-size: 0.8rem; white-space: nowrap; }
</style>
</head>
<body>
<div class="header">
  <span style="font-size:1.5rem">&#9729;</span>
  <h1>OneDrive SyncServer</h1>
</div>
<div class="container">

  <div class="card">
    <h2>Status</h2>
    <div class="status-grid">
      <div class="status-item">
        <div class="label">Verbindung</div>
        <div class="value {% if authenticated %}ok{% else %}error-c{% endif %}">
          {% if authenticated %}&#10003; Authentifiziert{% else %}&#10007; Nicht verbunden{% endif %}
        </div>
      </div>
      <div class="status-item">
        <div class="label">Letzter Sync</div>
        <div class="value" id="last-sync">{{ status.last_sync or "Noch kein Sync" }}</div>
      </div>
      <div class="status-item">
        <div class="label">Dateien synchronisiert</div>
        <div class="value" id="files-synced">{{ status.files_synced }}</div>
      </div>
    </div>
    {% if status.errors %}{% for err in status.errors[-3:] %}<div class="error-item">&#9888; {{ err }}</div>{% endfor %}{% endif %}
    <div class="progress-box" id="progress-box">
      <div class="label"><span class="spinner"></span>Aktiv - zuletzt aktualisiert: <span id="progress-time"></span></div>
      <div class="detail" id="progress-detail"></div>
    </div>
  </div>

  {% if not authenticated %}
  <div class="card">
    <h2>Microsoft Anmeldung</h2>
    <div class="auth-box">
      {% if auth_url %}
      <p><strong>Schritt 2:</strong> Klicke auf die URL um sie zu kopieren. Im Browser anmelden. Danach die komplette URL aus der Adressleiste kopieren (auch bei "wrongplace"-Seite) und unten einsetzen.</p>
      <div class="auth-url-box" onclick="navigator.clipboard.writeText(this.textContent).then(()=>showToast('URL kopiert'))">{{ auth_url }}</div>
      <input type="text" class="auth-input" id="auth-code" placeholder="Antwort-URL einfuegen...">
      <button class="btn btn-primary" onclick="submitAuth()">Bestaetigen</button>
      {% if debug_log %}
      <p style="color:#6b7280;font-size:0.75rem;margin-top:12px">Debug-Log:</p>
      <div class="auth-debug-box">{{ debug_log }}</div>
      {% endif %}
      {% else %}
      <p><strong>Schritt 1:</strong> Autorisierungs-Link generieren.</p>
      <button class="btn btn-primary" id="auth-btn" onclick="startAuth()">&#128279; Autorisierungs-Link generieren</button>
      {% endif %}
    </div>
  </div>
  {% endif %}

  {% if authenticated %}
  <div class="card">
    <h2>Ordner Konfiguration</h2>
    <p class="hint">Unterordner werden erst beim Aufklappen (&#9656;) geladen. Abgewaehlte Ordner werden beim Sync komplett uebersprungen - auch verschachtelt.</p>
    <div class="folder-tree" id="folder-root" data-path=""></div>
  </div>

  <div class="card">
    <h2>Datei suchen &amp; herunterladen</h2>
    <p style="color:#9ca3af;font-size:0.85rem;margin-bottom:16px">Sucht direkt in OneDrive. Laedt exakt eine Datei nach /share/onedrive_downloads/.</p>
    <p style="color:#6b7280;font-size:0.78rem;margin-bottom:8px;font-weight:600">SUCHE (nur Dateiname)</p>
    <div class="dl-row">
      <input type="text" id="search-name" placeholder="Dateiname (z.B. urlaub.jpg)">
      <button class="btn btn-primary" onclick="searchFile()">&#128269; Suchen</button>
    </div>
    <div id="search-results" class="search-results"></div>
    <hr style="border-color:#374151;margin:16px 0">
    <p style="color:#6b7280;font-size:0.78rem;margin-bottom:8px;font-weight:600">DIREKTER DOWNLOAD (Pfad + Dateiname)</p>
    <div class="dl-row">
      <input type="text" id="dl-path" placeholder="OneDrive-Pfad (z.B. Fotos/2024/Italien)">
      <input type="text" id="dl-file" placeholder="Dateiname (z.B. urlaub.jpg)">
      <button class="btn btn-success" onclick="downloadByPath()">&#8659; Herunterladen</button>
    </div>
    <div id="dl-result" class="dl-result"></div>
  </div>

  <div class="card">
    <h2>Sync Log</h2>
    <div class="log-box">{{ log }}</div>
  </div>
  {% endif %}

</div>
<div class="save-bar">
  {% if authenticated %}
  <button class="btn btn-success" onclick="triggerSync()">&#8635; Jetzt synchronisieren</button>
  {% endif %}
  <button class="btn btn-primary" onclick="saveConfig()">&#128190; Konfiguration speichern</button>
</div>
<div class="toast" id="toast"></div>

<script>
const BASE = "{{ base }}";
function apiUrl(path) { return BASE + path; }
const CONFIG = {{ config | tojson }};
const FILTER_OPTIONS = {{ filter_options | tojson }};
const DELETE_OPTIONS = {{ delete_options | tojson }};

let pendingChanges = {};
function showToast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok ? '#10b981' : '#ef4444';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}
function updateConfig(path, key, value) {
  if (!pendingChanges[path]) pendingChanges[path] = {};
  pendingChanges[path][key] = value;
}
function toggleFolder(path, enabled) { updateConfig(path, 'sync', enabled); }

function getCfg(path) {
  return CONFIG[path] || {};
}

function buildFolderRow(folder, depth) {
  const cfg = getCfg(folder.path);
  const enabled = cfg.sync !== undefined ? cfg.sync : true;

  const item = document.createElement('div');
  item.className = 'folder-item';

  const row = document.createElement('div');
  row.className = 'folder-row';

  for (let i = 0; i < depth; i++) {
    const indent = document.createElement('div');
    indent.className = 'folder-indent';
    row.appendChild(indent);
  }

  const expand = document.createElement('div');
  expand.className = 'folder-expand' + (folder.has_children ? '' : ' leaf');
  expand.textContent = String.fromCharCode(9654);
  row.appendChild(expand);

  const label = document.createElement('label');
  label.className = 'checkbox-label';
  const checkbox = document.createElement('input');
  checkbox.type = 'checkbox';
  checkbox.checked = enabled;
  checkbox.addEventListener('change', function() { toggleFolder(folder.path, this.checked); });
  label.appendChild(checkbox);
  row.appendChild(label);

  const nameDiv = document.createElement('div');
  nameDiv.className = 'folder-name' + (enabled ? '' : ' disabled');
  nameDiv.textContent = String.fromCodePoint(128193) + ' ' + folder.name;
  row.appendChild(nameDiv);

  if (enabled) {
    const controls = document.createElement('div');
    controls.className = 'folder-controls';

    const filterSel = document.createElement('select');
    FILTER_OPTIONS.forEach(function(opt) {
      const o = document.createElement('option');
      o.value = opt.value; o.textContent = opt.label;
      if ((cfg.filter || 'all') === opt.value) o.selected = true;
      filterSel.appendChild(o);
    });
    filterSel.addEventListener('change', function() { updateConfig(folder.path, 'filter', this.value); });
    controls.appendChild(filterSel);

    const deleteSel = document.createElement('select');
    DELETE_OPTIONS.forEach(function(opt) {
      const o = document.createElement('option');
      o.value = opt.value; o.textContent = opt.label;
      if ((cfg.delete_after || 'never') === opt.value) o.selected = true;
      deleteSel.appendChild(o);
    });
    deleteSel.addEventListener('change', function() { updateConfig(folder.path, 'delete_after', this.value); });
    controls.appendChild(deleteSel);

    row.appendChild(controls);
  }

  item.appendChild(row);

  const childrenContainer = document.createElement('div');
  childrenContainer.className = 'folder-children';
  item.appendChild(childrenContainer);

  let loaded = false;
  if (folder.has_children) {
    expand.addEventListener('click', async function() {
      const isOpen = childrenContainer.classList.contains('open');
      if (isOpen) {
        childrenContainer.classList.remove('open');
        expand.textContent = String.fromCharCode(9654);
        return;
      }
      childrenContainer.classList.add('open');
      expand.textContent = String.fromCharCode(9660);
      if (!loaded) {
        loaded = true;
        childrenContainer.innerHTML = '<div class="folder-loading">Laedt...</div>';
        try {
          const res = await fetch(apiUrl('/api/folder_children'), {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({path: folder.path})
          });
          const d = await res.json();
          childrenContainer.innerHTML = '';
          if (d.success) {
            d.folders.forEach(function(child) {
              childrenContainer.appendChild(buildFolderRow(child, depth + 1));
            });
            if (d.folders.length === 0) {
              childrenContainer.innerHTML = '<div class="folder-loading">Keine Unterordner</div>';
            }
          } else {
            childrenContainer.innerHTML = '<div class="folder-loading">Fehler: ' + (d.error || 'unbekannt') + '</div>';
          }
        } catch (e) {
          childrenContainer.innerHTML = '<div class="folder-loading">Fehler beim Laden</div>';
        }
      }
    });
  }

  return item;
}

async function loadRootFolders() {
  const container = document.getElementById('folder-root');
  if (!container) return;
  container.innerHTML = '<div class="folder-loading">Laedt Ordner...</div>';
  try {
    const res = await fetch(apiUrl('/api/folder_children'), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: ''})
    });
    const d = await res.json();
    container.innerHTML = '';
    if (d.success) {
      if (d.folders.length === 0) {
        container.innerHTML = '<p style="color:#6b7280;font-size:0.85rem">Keine Ordner gefunden.</p>';
      }
      d.folders.forEach(function(folder) {
        container.appendChild(buildFolderRow(folder, 0));
      });
    } else {
      container.innerHTML = '<p style="color:#ef4444;font-size:0.85rem">Fehler: ' + (d.error || 'unbekannt') + '</p>';
    }
  } catch (e) {
    container.innerHTML = '<p style="color:#ef4444;font-size:0.85rem">Fehler beim Laden der Ordner</p>';
  }
}
if (document.getElementById('folder-root')) {
  loadRootFolders();
}

async function startAuth() {
  const btn = document.getElementById('auth-btn');
  if (btn) { btn.textContent = 'Wird generiert...'; btn.disabled = true; }
  const res = await fetch(apiUrl('/auth/start'), {method: 'POST', cache: 'no-store'});
  if (res.ok) { location.reload(); }
  else {
    const d = await res.json();
    showToast('Fehler: ' + (d.error || 'unbekannt'), false);
    if (btn) { btn.textContent = 'Autorisierungs-Link generieren'; btn.disabled = false; }
  }
}
async function submitAuth() {
  const code = document.getElementById('auth-code').value.trim();
  if (!code) return;
  showToast('Wird verarbeitet...');
  const res = await fetch(apiUrl('/auth/complete'), {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({response_url: code}),
    cache: 'no-store'
  });
  const d = await res.json();
  if (res.ok) { showToast('Authentifizierung erfolgreich'); setTimeout(() => location.reload(), 1500); }
  else {
    showToast('Fehler: ' + (d.error_short || d.error || 'unbekannt'), false);
    setTimeout(() => location.reload(), 2000);
  }
}
async function saveConfig() {
  const res = await fetch(apiUrl('/api/config'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(pendingChanges)
  });
  if (res.ok) { showToast('Konfiguration gespeichert'); pendingChanges = {}; }
  else { showToast('Fehler beim Speichern', false); }
}
async function triggerSync() {
  showToast('Sync gestartet - laeuft im Hintergrund, Fortschritt siehe oben...');
  await fetch(apiUrl('/api/sync'), {method: 'POST'});
  pollProgress();
}
async function searchFile() {
  const name = document.getElementById('search-name').value.trim();
  if (!name) return;
  const container = document.getElementById('search-results');
  container.innerHTML = '<p style="color:#9ca3af;font-size:0.85rem">Suche laeuft...</p>';
  const res = await fetch(apiUrl('/api/search'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({filename: name})
  });
  const d = await res.json();
  if (!res.ok) { container.innerHTML = '<p style="color:#ef4444;font-size:0.85rem">Fehler: ' + (d.error||'unbekannt') + '</p>'; return; }
  if (d.found === 0) { container.innerHTML = '<p style="color:#f59e0b;font-size:0.85rem">Keine Treffer.</p>'; return; }
  container.innerHTML = '<p style="color:#9ca3af;font-size:0.78rem;margin-bottom:8px">' + d.found + ' Treffer:</p>';
  d.locations.forEach(function(loc) {
    var row = document.createElement('div');
    row.className = 'search-result-item';
    var info = document.createElement('div');
    info.innerHTML = '<div>' + loc.name + '</div><div class="path">' + loc.path + '</div>';
    var btn = document.createElement('button');
    btn.className = 'dl-btn';
    btn.textContent = String.fromCharCode(8659) + ' Download';
    btn.addEventListener('click', function() { downloadById(loc.item_id, loc.name); });
    row.appendChild(info);
    row.appendChild(btn);
    container.appendChild(row);
  });
}
async function downloadById(itemId, filename) {
  showToast('Download gestartet...');
  const res = await fetch(apiUrl('/api/download_by_id'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({item_id: itemId, filename: filename})
  });
  const d = await res.json();
  if (res.ok) { showToast('Gespeichert: ' + d.local_path); }
  else { showToast('Fehler: ' + (d.error||'unbekannt'), false); }
}
async function downloadByPath() {
  const path = document.getElementById('dl-path').value.trim();
  const file = document.getElementById('dl-file').value.trim();
  const result = document.getElementById('dl-result');
  if (!path || !file) { showToast('Pfad und Dateiname benoetigt', false); return; }
  result.textContent = 'Wird heruntergeladen...'; result.className = 'dl-result';
  const res = await fetch(apiUrl('/api/download_by_path'), {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: path, filename: file})
  });
  const d = await res.json();
  if (res.ok) { result.textContent = 'Gespeichert: ' + d.local_path; result.className = 'dl-result ok'; }
  else { result.textContent = 'Fehler: ' + (d.error||'unbekannt'); result.className = 'dl-result err'; }
}

let progressPollTimer = null;
async function pollProgress() {
  if (progressPollTimer) clearTimeout(progressPollTimer);
  try {
    const res = await fetch(apiUrl('/api/progress'), {cache: 'no-store'});
    const p = await res.json();
    const box = document.getElementById('progress-box');
    if (p.active) {
      box.classList.add('show');
      document.getElementById('progress-time').textContent = p.updated_at || '';
      document.getElementById('progress-detail').textContent = p.detail || '';
      progressPollTimer = setTimeout(pollProgress, 3000);
    } else {
      box.classList.remove('show');
      // Sync gerade zu Ende gegangen - Status/Ordnerliste einmalig aktualisieren
      refreshStatus();
    }
  } catch(e) {
    progressPollTimer = setTimeout(pollProgress, 5000);
  }
}
async function refreshStatus() {
  try {
    const res = await fetch(apiUrl('/api/status'), {cache: 'no-store'});
    const data = await res.json();
    if (document.getElementById('last-sync')) document.getElementById('last-sync').textContent = data.last_sync || 'Noch kein Sync';
    if (document.getElementById('files-synced')) document.getElementById('files-synced').textContent = data.files_synced;
  } catch(e) {}
}
// Beim Laden pruefen ob gerade schon ein Sync laeuft (z.B. periodischer Timer)
pollProgress();
setInterval(refreshStatus, 30000);
</script>
</body>
</html>'''


@app.route('/')
def index():
    config = load_sync_config()
    status = load_status()
    authenticated = is_authenticated()
    base = get_base()
    auth_url = None
    debug_log = None
    if not authenticated:
        if os.path.exists(AUTH_URL_FILE):
            with open(AUTH_URL_FILE) as f:
                auth_url = f.read().strip()
        if os.path.exists(AUTH_DEBUG_LOG):
            with open(AUTH_DEBUG_LOG) as f:
                debug_log = f.read()
    log = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            log = "".join(f.readlines()[-50:])
    return render_template_string(
        HTML_TEMPLATE,
        config=config, status=status, authenticated=authenticated,
        filter_options=FILTER_OPTIONS,
        delete_options=DELETE_OPTIONS, auth_url=auth_url,
        debug_log=debug_log, log=log, base=base
    )


@app.route('/api/folder_children', methods=['POST'])
def folder_children():
    data = request.json or {}
    path = data.get('path', '').strip('/')
    try:
        folders = get_onedrive_folder_children(path)
        return jsonify({"success": True, "folders": folders})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/auth/start', methods=['POST'])
def auth_start():
    global _auth_proc
    if os.path.exists(AUTH_DEBUG_LOG):
        os.remove(AUTH_DEBUG_LOG)
    auth_log("=== Neuer Auth-Versuch (URL-Methode) ===")
    try:
        os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
        for f in [AUTH_URL_FILE, RESPONSE_URL_FILE]:
            if os.path.exists(f):
                os.remove(f)

        if _auth_proc and _auth_proc.poll() is None:
            _auth_proc.kill()
            _auth_proc.wait()

        _auth_proc = subprocess.Popen(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR,
             "--auth-files", f"{AUTH_URL_FILE}:{RESPONSE_URL_FILE}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(30):
            time.sleep(0.5)
            if os.path.exists(AUTH_URL_FILE):
                break
        if os.path.exists(AUTH_URL_FILE):
            with open(AUTH_URL_FILE) as f:
                url = f.read().strip()
            auth_log(f"authUrl erstellt, Laenge: {len(url)}, Prozess PID {_auth_proc.pid} bleibt aktiv")
            if url:
                return jsonify({"success": True, "url": url})
        auth_log("FEHLER: authUrl nicht erstellt")
        return jsonify({"success": False, "error": "authUrl nicht erstellt"}), 500
    except Exception as e:
        auth_log(f"EXCEPTION: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/auth/complete', methods=['POST'])
def auth_complete():
    global _auth_proc
    data = request.json
    response_url = data.get('response_url', '').strip()
    auth_log("=== auth/complete ===")
    auth_log(f"Laenge: {len(response_url)}, Anfang: {response_url[:80]}")
    try:
        if not os.path.exists(AUTH_URL_FILE):
            return jsonify({"success": False, "error": "Session abgelaufen - Link neu generieren", "error_short": "Session abgelaufen"}), 400
        if not _auth_proc or _auth_proc.poll() is not None:
            auth_log("FEHLER: Auth-Prozess laeuft nicht mehr - Link neu generieren noetig")
            return jsonify({"success": False, "error": "Auth-Session verloren - bitte neuen Link generieren", "error_short": "Session verloren, neu generieren"}), 400

        with open(RESPONSE_URL_FILE, 'w') as f:
            f.write(response_url)
        auth_log("responseUrl geschrieben, warte auf laufenden Prozess...")

        try:
            stdout, stderr = _auth_proc.communicate(timeout=30)
            stdout_s = stdout.decode(errors='replace')
            stderr_s = stderr.decode(errors='replace')
            auth_log(f"exit: {_auth_proc.returncode}, stdout: {stdout_s[:500]}, stderr: {stderr_s[:500]}")
        except subprocess.TimeoutExpired:
            auth_log("Timeout beim Warten auf Auth-Prozess")
            _auth_proc.kill()
            _auth_proc = None
            return jsonify({"success": False, "error": "Zeitueberschreitung beim Austausch", "error_short": "Timeout"}), 400

        if os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token"):
            auth_log("SUCCESS")
            _auth_proc = None
            return jsonify({"success": True})

        short_err = "Unbekannter Fehler"
        combined = stdout_s + stderr_s
        for line in combined.splitlines():
            if "AADSTS" in line or "Error Reason" in line:
                short_err = line.strip()[:80]
                break
        _auth_proc = None
        return jsonify({"success": False, "error": combined, "error_short": short_err}), 400
    except Exception as e:
        auth_log(f"EXCEPTION: {e}")
        _auth_proc = None
        return jsonify({"success": False, "error": str(e), "error_short": str(e)[:80]}), 500


@app.route('/api/search', methods=['POST'])
def search_file():
    data = request.json
    filename = data.get('filename', '').strip()
    if not filename:
        return jsonify({"success": False, "error": "filename benoetigt"}), 400
    try:
        token = get_access_token()
        encoded = urllib.parse.quote(filename)
        results = graph_get(token, f"/me/drive/root/search(q='{encoded}')")
        items = results.get("value", [])
        needle = filename.lower()
        matches = [i for i in items if needle in i.get("name", "").lower() and "folder" not in i]
        locations = []
        for item in matches:
            parent = item.get("parentReference", {})
            parent_path = parent.get("path", "")
            clean_path = parent_path.split("root:", 1)[1].strip("/") if "root:" in parent_path else parent_path
            locations.append({"item_id": item["id"], "name": item["name"],
                               "path": clean_path or "/", "size": item.get("size", 0)})
        return jsonify({"success": True, "found": len(locations), "locations": locations})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/download_by_id', methods=['POST'])
def download_by_id():
    data = request.json
    item_id = data.get('item_id', '').strip()
    filename = data.get('filename', '').strip()
    if not item_id or not filename:
        return jsonify({"success": False, "error": "item_id und filename benoetigt"}), 400
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        token = get_access_token()
        dest = os.path.join(DOWNLOAD_DIR, filename)
        graph_download(token, item_id, dest)
        return jsonify({"success": True, "local_path": dest})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/download_by_path', methods=['POST'])
def download_by_path():
    data = request.json
    onedrive_path = data.get('path', '').strip('/')
    filename = data.get('filename', '').strip()
    if not onedrive_path or not filename:
        return jsonify({"success": False, "error": "path und filename benoetigt"}), 400
    try:
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        token = get_access_token()
        encoded_path = urllib.parse.quote(f"{onedrive_path}/{filename}")
        item = graph_get(token, f"/me/drive/root:/{encoded_path}")
        dest = os.path.join(DOWNLOAD_DIR, filename)
        graph_download(token, item["id"], dest)
        return jsonify({"success": True, "local_path": dest})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/config', methods=['POST'])
def update_config():
    changes = request.json
    config = load_sync_config()
    for path, updates in changes.items():
        if path not in config:
            config[path] = {"sync": True, "filter": "all", "delete_after": "never", "custom_local_path": None}
        config[path].update(updates)
    save_sync_config(config)
    return jsonify({"success": True})


@app.route('/api/status')
def get_status():
    return jsonify(load_status())


@app.route('/api/progress')
def get_progress():
    return jsonify(load_progress())


@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    def run_sync():
        try:
            # Kein enges Zeit-Limit mehr - sync_manager.py ueberwacht selbst
            # Aktivitaet (STALL_TIMEOUT_SECONDS). 12h nur als absoluter
            # Notfall-Deckel gegen echte Zombie-Prozesse.
            subprocess.run(["python3", "/app/sync_manager.py"], timeout=43200)
        except subprocess.TimeoutExpired:
            auth_log("Sync-Wrapper Notfall-Timeout nach 12h - Sync abgebrochen")
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({"success": True})


if __name__ == '__main__':
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=PORT, debug=False)
