#!/usr/bin/env python3
"""OneDrive SyncServer - Web UI Backend"""

import json
import os
import subprocess
import threading
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

CONFIG_DIR = "/data"
SYNC_CONFIG = f"{CONFIG_DIR}/sync_config.json"
ONEDRIVE_CONFIG_DIR = f"{CONFIG_DIR}/onedrive"
AUTH_URL_FILE = f"{ONEDRIVE_CONFIG_DIR}/authUrl"
RESPONSE_URL_FILE = f"{ONEDRIVE_CONFIG_DIR}/responseUrl"
SHARE_DIR = "/share/onedrive"
LOG_FILE = f"{CONFIG_DIR}/sync.log"
STATUS_FILE = f"{CONFIG_DIR}/sync_status.json"

def load_sync_config():
    if os.path.exists(SYNC_CONFIG):
        with open(SYNC_CONFIG) as f:
            return json.load(f)
    return {}

def save_sync_config(config):
    with open(SYNC_CONFIG, 'w') as f:
        json.dump(config, f, indent=2)

def load_status():
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE) as f:
            return json.load(f)
    return {"last_sync": None, "files_synced": 0, "errors": [], "authenticated": False}

def is_authenticated():
    return os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token")

def get_local_folders():
    """List all folders under /share/onedrive recursively"""
    folders = []
    if not os.path.exists(SHARE_DIR):
        return folders
    for root, dirs, files in os.walk(SHARE_DIR):
        for d in sorted(dirs):
            full = os.path.join(root, d)
            rel = os.path.relpath(full, SHARE_DIR)
            folders.append(rel)
    return sorted(folders)

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

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OneDrive SyncServer</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #111827; color: #f9fafb; min-height: 100vh; }
  .header { background: #1f2937; border-bottom: 1px solid #374151;
             padding: 16px 24px; display: flex; align-items: center; gap: 12px; }
  .header h1 { font-size: 1.25rem; font-weight: 600; color: #f9fafb; }
  .header .icon { font-size: 1.5rem; }
  .container { max-width: 1000px; margin: 0 auto; padding: 24px; }
  .card { background: #1f2937; border: 1px solid #374151; border-radius: 12px;
           padding: 20px; margin-bottom: 20px; }
  .card h2 { font-size: 1rem; font-weight: 600; color: #9ca3af;
              text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 16px; }
  .status-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .status-item { background: #111827; border-radius: 8px; padding: 12px; }
  .status-item .label { font-size: 0.75rem; color: #6b7280; margin-bottom: 4px; }
  .status-item .value { font-size: 1rem; font-weight: 600; color: #f9fafb; }
  .status-item .value.ok { color: #10b981; }
  .status-item .value.error { color: #ef4444; }
  .auth-box { background: #111827; border: 1px solid #3b82f6; border-radius: 8px; padding: 16px; }
  .auth-box p { color: #9ca3af; margin-bottom: 12px; font-size: 0.9rem; }
  .auth-url-box { background: #1f2937; border: 1px solid #374151; border-radius: 6px;
                   padding: 10px; margin-bottom: 12px; word-break: break-all;
                   font-size: 0.8rem; color: #60a5fa; }
  .btn { padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
          font-size: 0.9rem; font-weight: 500; transition: all 0.15s; }
  .btn-primary { background: #3b82f6; color: white; }
  .btn-primary:hover { background: #2563eb; }
  .btn-success { background: #10b981; color: white; }
  .btn-success:hover { background: #059669; }
  .auth-input { width: 100%; background: #1f2937; border: 1px solid #374151;
                 color: #f9fafb; padding: 8px 12px; border-radius: 6px;
                 font-size: 0.9rem; margin: 8px 0; }
  .folder-item { border-bottom: 1px solid #374151; }
  .folder-item:last-child { border-bottom: none; }
  .folder-row { display: flex; align-items: center; gap: 8px; padding: 10px 8px; flex-wrap: wrap; }
  .folder-row:hover { background: #111827; border-radius: 6px; }
  .folder-indent { width: 20px; flex-shrink: 0; }
  .folder-name { flex: 1; font-size: 0.9rem; color: #e5e7eb; min-width: 150px; }
  .folder-name.disabled { color: #6b7280; }
  .folder-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  select { background: #111827; border: 1px solid #374151; color: #f9fafb;
            padding: 4px 8px; border-radius: 6px; font-size: 0.8rem; }
  input[type=text] { background: #111827; border: 1px solid #374151; color: #f9fafb;
                      padding: 4px 8px; border-radius: 6px; font-size: 0.8rem; width: 200px; }
  .checkbox-label { display: flex; align-items: center; gap: 6px; cursor: pointer;
                     font-size: 0.85rem; color: #9ca3af; }
  .log-box { background: #111827; border-radius: 8px; padding: 12px;
              font-family: monospace; font-size: 0.78rem; color: #9ca3af;
              max-height: 200px; overflow-y: auto; white-space: pre-wrap; }
  .save-bar { position: sticky; bottom: 0; background: #1f2937;
               border-top: 1px solid #374151; padding: 16px 24px;
               display: flex; justify-content: flex-end; gap: 12px; }
  .toast { position: fixed; bottom: 80px; right: 24px; background: #10b981;
             color: white; padding: 12px 20px; border-radius: 8px;
             font-size: 0.9rem; opacity: 0; transition: opacity 0.3s;
             pointer-events: none; z-index: 100; }
  .toast.show { opacity: 1; }
  .error-item { color: #ef4444; font-size: 0.8rem; padding: 4px 0; }
</style>
</head>
<body>
<div class="header">
  <span class="icon">&#x2601;&#xFE0F;</span>
  <h1>OneDrive SyncServer</h1>
</div>
<div class="container">

  <div class="card">
    <h2>Status</h2>
    <div class="status-grid">
      <div class="status-item">
        <div class="label">Verbindung</div>
        <div class="value {{ \'ok\' if authenticated else \'error\' }}">
          {{ "&#x2713; Authentifiziert" if authenticated else "&#x2717; Nicht verbunden" }}
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
    {% if status.errors %}
      {% for err in status.errors[-3:] %}
      <div class="error-item">&#x26A0; {{ err }}</div>
      {% endfor %}
    {% endif %}
  </div>

  {% if not authenticated %}
  <div class="card">
    <h2>Microsoft Anmeldung</h2>
    <div class="auth-box">
      {% if auth_url %}
      <p>Schritt 2: &#214;ffne diesen Link in einem Browser, melde dich bei Microsoft an, und kopiere danach die Antwort-URL in das Feld unten.</p>
      <div class="auth-url-box">{{ auth_url }}</div>
      <input type="text" class="auth-input" id="auth-code" placeholder="Antwort-URL von Microsoft eingeben (https://login.microsoftonline.com/...)">
      <button class="btn btn-primary" onclick="submitAuth()">Best&#228;tigen</button>
      {% else %}
      <p>Schritt 1: Klicke auf den Button um den Autorisierungs-Link zu generieren.</p>
      <button class="btn btn-primary" onclick="startAuth()">&#x1F517; Autorisierungs-Link generieren</button>
      {% endif %}
    </div>
  </div>
  {% endif %}

  {% if authenticated %}
  <div class="card">
    <h2>Ordner Konfiguration</h2>
    <div class="folder-tree">
      {% for folder in folders %}
      {% set cfg = config.get(folder, {}) %}
      {% set depth = folder.count("/") %}
      {% set enabled = cfg.get("sync", True) %}
      <div class="folder-item">
        <div class="folder-row">
          {% for i in range(depth) %}<div class="folder-indent"></div>{% endfor %}
          <label class="checkbox-label">
            <input type="checkbox" {{ "checked" if enabled else "" }}
                   onchange="toggleFolder(\'{{ folder }}\', this.checked)">
          </label>
          <div class="folder-name {{ \'\' if enabled else \'disabled\' }}">&#x1F4C1; {{ folder.split(\'/\')[-1] }}</div>
          {% if enabled %}
          <div class="folder-controls">
            <select onchange="updateConfig(\'{{ folder }}\', \'filter\', this.value)">
              {% for opt in filter_options %}
              <option value="{{ opt.value }}" {{ "selected" if cfg.get(\'filter\', \'all\') == opt.value else "" }}>{{ opt.label }}</option>
              {% endfor %}
            </select>
            <select onchange="updateConfig(\'{{ folder }}\', \'delete_after\', this.value)">
              {% for opt in delete_options %}
              <option value="{{ opt.value }}" {{ "selected" if cfg.get(\'delete_after\', \'never\') == opt.value else "" }}>{{ opt.label }}</option>
              {% endfor %}
            </select>
            <label class="checkbox-label">
              <input type="checkbox" {{ "checked" if not cfg.get(\'custom_local_path\') else "" }}
                     onchange="toggleCustomPath(\'{{ folder }}\', this.checked)">
              Standard-Pfad
            </label>
            {% if cfg.get("custom_local_path") %}
            <input type="text" placeholder="/share/paperless/media"
                   value="{{ cfg.get(\'custom_local_path\', \'\') }}"
                   onchange="updateConfig(\'{{ folder }}\', \'custom_local_path\', this.value)">
            {% endif %}
          </div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>
  <div class="card">
    <h2>Sync Log</h2>
    <div class="log-box">{{ log }}</div>
  </div>
  {% endif %}

</div>
<div class="save-bar">
  {% if authenticated %}
  <button class="btn btn-success" onclick="triggerSync()">&#x1F504; Jetzt synchronisieren</button>
  {% endif %}
  <button class="btn btn-primary" onclick="saveConfig()">&#x1F4BE; Konfiguration speichern</button>
</div>
<div class="toast" id="toast"></div>

<script>
let pendingChanges = {};
function showToast(msg, ok=true) {
  const t = document.getElementById(\'toast\');
  t.textContent = msg;
  t.style.background = ok ? \'#10b981\' : \'#ef4444\';
  t.classList.add(\'show\');
  setTimeout(() => t.classList.remove(\'show\'), 3000);
}
function updateConfig(path, key, value) {
  if (!pendingChanges[path]) pendingChanges[path] = {};
  pendingChanges[path][key] = value;
}
function toggleFolder(path, enabled) { updateConfig(path, \'sync\', enabled); }
function toggleCustomPath(path, useStandard) {
  updateConfig(path, \'custom_local_path\', useStandard ? null : \'/share/\');
}
async function startAuth() {
  showToast(\'Generiere Link...\');
  const res = await fetch(\'/auth/start\', {method: \'POST\'});
  if (res.ok) { location.reload(); }
  else { showToast(\'Fehler beim Generieren\', false); }
}
async function submitAuth() {
  const code = document.getElementById(\'auth-code\').value.trim();
  if (!code) return;
  const res = await fetch(\'/auth/complete\', {
    method: \'POST\',
    headers: {\'Content-Type\': \'application/json\'},
    body: JSON.stringify({response_url: code})
  });
  if (res.ok) { showToast(\'Authentifizierung erfolgreich\'); setTimeout(() => location.reload(), 1500); }
  else { showToast(\'Authentifizierung fehlgeschlagen\', false); }
}
async function saveConfig() {
  const res = await fetch(\'/api/config\', {
    method: \'POST\',
    headers: {\'Content-Type\': \'application/json\'},
    body: JSON.stringify(pendingChanges)
  });
  if (res.ok) { showToast(\'Konfiguration gespeichert\'); pendingChanges = {}; }
  else { showToast(\'Fehler beim Speichern\', false); }
}
async function triggerSync() {
  showToast(\'Sync gestartet...\');
  await fetch(\'/api/sync\', {method: \'POST\'});
  setTimeout(() => location.reload(), 3000);
}
setInterval(async () => {
  const res = await fetch(\'/api/status\');
  const data = await res.json();
  document.getElementById(\'last-sync\').textContent = data.last_sync || \'Noch kein Sync\';
  document.getElementById(\'files-synced\').textContent = data.files_synced;
}, 30000);
</script>
</body>
</html>
'''

@app.route('/')
def index():
    config = load_sync_config()
    status = load_status()
    authenticated = is_authenticated()
    folders = get_local_folders() if authenticated else []
    auth_url = None
    if not authenticated and os.path.exists(AUTH_URL_FILE):
        with open(AUTH_URL_FILE) as f:
            auth_url = f.read().strip()
    log = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            log = "".join(f.readlines()[-50:])
    return render_template_string(
        HTML_TEMPLATE,
        config=config,
        status=status,
        authenticated=authenticated,
        folders=folders,
        filter_options=FILTER_OPTIONS,
        delete_options=DELETE_OPTIONS,
        auth_url=auth_url,
        log=log
    )

@app.route('/auth/start', methods=['POST'])
def auth_start():
    """Generate OneDrive OAuth URL and write to file"""
    try:
        os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
        # Remove old files
        for f in [AUTH_URL_FILE, RESPONSE_URL_FILE]:
            if os.path.exists(f):
                os.remove(f)
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR,
             "--auth-files", f"{AUTH_URL_FILE}:{RESPONSE_URL_FILE}"],
            capture_output=True, text=True, timeout=30
        )
        if os.path.exists(AUTH_URL_FILE):
            with open(AUTH_URL_FILE) as f:
                url = f.read().strip()
            if url:
                return jsonify({"success": True, "url": url})
        # Fallback: search in stdout/stderr
        for line in (result.stdout + result.stderr).splitlines():
            if "https://login.microsoftonline.com" in line or "https://login.live.com" in line:
                return jsonify({"success": True, "url": line.strip()})
        return jsonify({"success": False, "error": result.stderr}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/auth/complete', methods=['POST'])
def auth_complete():
    """Complete OAuth by writing response URL to file and re-running onedrive"""
    data = request.json
    response_url = data.get('response_url', '')
    try:
        with open(RESPONSE_URL_FILE, 'w') as f:
            f.write(response_url)
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR,
             "--auth-files", f"{AUTH_URL_FILE}:{RESPONSE_URL_FILE}"],
            capture_output=True, text=True, timeout=60
        )
        if os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token"):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": result.stderr}), 400
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

@app.route('/api/sync', methods=['POST'])
def trigger_sync():
    def run_sync():
        subprocess.run(["python3", "/app/sync_manager.py"], timeout=300)
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({"success": True})

if __name__ == '__main__':
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=8765, debug=False)
