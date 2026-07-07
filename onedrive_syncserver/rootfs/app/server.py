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

def get_onedrive_folders():
    """List top-level OneDrive folders via onedrive CLI"""
    if not is_authenticated():
        return []
    try:
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR, "--list-shared-items"],
            capture_output=True, text=True, timeout=30
        )
        # Parse folder listing - onedrive outputs one path per line
        folders = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.startswith("["):
                folders.append(line)
        return folders
    except Exception as e:
        return []

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
  .status-item .value.warn { color: #f59e0b; }
  .auth-box { background: #111827; border: 1px solid #3b82f6; border-radius: 8px;
               padding: 16px; }
  .auth-box p { color: #9ca3af; margin-bottom: 12px; font-size: 0.9rem; }
  .auth-link { display: inline-block; background: #3b82f6; color: white;
                padding: 8px 16px; border-radius: 6px; text-decoration: none;
                font-size: 0.9rem; margin-bottom: 12px; }
  .auth-input { width: 100%; background: #1f2937; border: 1px solid #374151;
                 color: #f9fafb; padding: 8px 12px; border-radius: 6px;
                 font-size: 0.9rem; margin-bottom: 8px; }
  .btn { padding: 8px 16px; border-radius: 6px; border: none; cursor: pointer;
          font-size: 0.9rem; font-weight: 500; transition: all 0.15s; }
  .btn-primary { background: #3b82f6; color: white; }
  .btn-primary:hover { background: #2563eb; }
  .btn-success { background: #10b981; color: white; }
  .btn-success:hover { background: #059669; }
  .btn-sm { padding: 4px 10px; font-size: 0.8rem; }
  .folder-tree { }
  .folder-item { border-bottom: 1px solid #374151; }
  .folder-item:last-child { border-bottom: none; }
  .folder-row { display: flex; align-items: center; gap: 8px; padding: 10px 8px;
                 flex-wrap: wrap; }
  .folder-row:hover { background: #111827; border-radius: 6px; }
  .folder-indent { width: 20px; flex-shrink: 0; }
  .folder-name { flex: 1; font-size: 0.9rem; color: #e5e7eb; min-width: 150px; }
  .folder-name.disabled { color: #6b7280; }
  .folder-controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  select { background: #111827; border: 1px solid #374151; color: #f9fafb;
            padding: 4px 8px; border-radius: 6px; font-size: 0.8rem; }
  input[type=text] { background: #111827; border: 1px solid #374151; color: #f9fafb;
                      padding: 4px 8px; border-radius: 6px; font-size: 0.8rem; width: 200px; }
  .inherit-badge { font-size: 0.7rem; color: #6b7280; background: #374151;
                    padding: 2px 6px; border-radius: 4px; }
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
  .error-list { margin-top: 8px; }
  .error-item { color: #ef4444; font-size: 0.8rem; padding: 4px 0; }
</style>
</head>
<body>
<div class="header">
  <span class="icon">☁️</span>
  <h1>OneDrive SyncServer</h1>
</div>

<div class="container">

  <!-- Status Card -->
  <div class="card">
    <h2>Status</h2>
    <div class="status-grid">
      <div class="status-item">
        <div class="label">Verbindung</div>
        <div class="value {{ \'ok\' if authenticated else \'error\' }}" id="auth-status">
          {{ "✓ Authentifiziert" if authenticated else "✗ Nicht verbunden" }}
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
    <div class="error-list">
      {% for err in status.errors[-3:] %}
      <div class="error-item">⚠ {{ err }}</div>
      {% endfor %}
    </div>
    {% endif %}
  </div>

  <!-- Auth Card (only if not authenticated) -->
  {% if not authenticated %}
  <div class="card">
    <h2>Microsoft Anmeldung</h2>
    <div class="auth-box">
      <p>Klicke auf den Link um OneDrive zu autorisieren. Kopiere danach den Code und füge ihn unten ein.</p>
      <a href="/auth/start" class="auth-link" target="_blank">🔗 OneDrive autorisieren</a>
      <br>
      <input type="text" class="auth-input" id="auth-code" placeholder="Code von Microsoft eingeben...">
      <button class="btn btn-primary" onclick="submitAuth()">Bestätigen</button>
    </div>
  </div>
  {% endif %}

  <!-- Folder Config Card -->
  {% if authenticated %}
  <div class="card">
    <h2>Ordner Konfiguration</h2>
    <div class="folder-tree" id="folder-tree">
      {% for folder in folders %}
      {% set cfg = config.get(folder, {}) %}
      {% set depth = folder.count("/") %}
      {% set enabled = cfg.get("sync", True) %}
      <div class="folder-item" data-path="{{ folder }}">
        <div class="folder-row">
          {% for i in range(depth) %}<div class="folder-indent"></div>{% endfor %}
          <label class="checkbox-label">
            <input type="checkbox" {{ "checked" if enabled else "" }}
                   onchange="toggleFolder(\'{{ folder }}\', this.checked)">
          </label>
          <div class="folder-name {{ \'\' if enabled else \'disabled\' }}">📁 {{ folder.split(\'/\')[-1] }}</div>
          {% if enabled %}
          <div class="folder-controls">
            <select onchange="updateConfig(\'{{ folder }}\', \'filter\', this.value)">
              {% for opt in filter_options %}
              <option value="{{ opt.value }}" {{ "selected" if cfg.get(\'filter\', \'all\') == opt.value else "" }}>
                {{ opt.label }}
              </option>
              {% endfor %}
            </select>
            {% if cfg.get("filter") == "custom" %}
            <input type="text" placeholder="pdf,docx,jpg..."
                   value="{{ cfg.get(\'custom_extensions\', \'\') }}"
                   onchange="updateConfig(\'{{ folder }}\', \'custom_extensions\', this.value)">
            {% endif %}
            <select onchange="updateConfig(\'{{ folder }}\', \'delete_after\', this.value)">
              {% for opt in delete_options %}
              <option value="{{ opt.value }}" {{ "selected" if cfg.get(\'delete_after\', \'never\') == opt.value else "" }}>
                {{ opt.label }}
              </option>
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

  <!-- Log Card -->
  <div class="card">
    <h2>Sync Log</h2>
    <div class="log-box" id="log-box">{{ log }}</div>
  </div>
  {% endif %}

</div>

<div class="save-bar">
  {% if authenticated %}
  <button class="btn btn-success" onclick="triggerSync()">🔄 Jetzt synchronisieren</button>
  {% endif %}
  <button class="btn btn-primary" onclick="saveConfig()">💾 Konfiguration speichern</button>
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

function toggleFolder(path, enabled) {
  updateConfig(path, \'sync\', enabled);
}

function toggleCustomPath(path, useStandard) {
  if (useStandard) {
    updateConfig(path, \'custom_local_path\', null);
  } else {
    updateConfig(path, \'custom_local_path\', \'/share/\');
  }
}

async function saveConfig() {
  const res = await fetch(\'/api/config\', {
    method: \'POST\',
    headers: {\'Content-Type\': \'application/json\'},
    body: JSON.stringify(pendingChanges)
  });
  if (res.ok) {
    showToast(\'✓ Konfiguration gespeichert\');
    pendingChanges = {};
  } else {
    showToast(\'✗ Fehler beim Speichern\', false);
  }
}

async function triggerSync() {
  showToast(\'⏳ Sync gestartet...\');
  const res = await fetch(\'/api/sync\', {method: \'POST\'});
  if (res.ok) {
    setTimeout(() => location.reload(), 3000);
  }
}

async function submitAuth() {
  const code = document.getElementById(\'auth-code\').value.trim();
  if (!code) return;
  const res = await fetch(\'/auth/complete\', {
    method: \'POST\',
    headers: {\'Content-Type\': \'application/json\'},
    body: JSON.stringify({code})
  });
  if (res.ok) {
    showToast(\'✓ Authentifizierung erfolgreich\');
    setTimeout(() => location.reload(), 1500);
  } else {
    showToast(\'✗ Authentifizierung fehlgeschlagen\', false);
  }
}

// Auto-refresh status every 30s
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
    log = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            lines = f.readlines()
            log = "".join(lines[-50:])
    return render_template_string(
        HTML_TEMPLATE,
        config=config,
        status=status,
        authenticated=authenticated,
        folders=folders,
        filter_options=FILTER_OPTIONS,
        delete_options=DELETE_OPTIONS,
        log=log
    )

@app.route('/auth/start')
def auth_start():
    """Start OneDrive OAuth - launch onedrive auth and return URL"""
    try:
        os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR, "--auth-files", "authUrl:responseUrl"],
            capture_output=True, text=True, timeout=30
        )
        # Extract auth URL from output
        for line in result.stdout.splitlines() + result.stderr.splitlines():
            if "https://login.microsoftonline.com" in line or "https://login.live.com" in line:
                url = line.strip()
                return f'<html><body style="background:#111827;color:#f9fafb;font-family:sans-serif;padding:40px">'\
                       f'<h2>OneDrive Autorisierung</h2>'\
                       f'<p style="margin:16px 0">Öffne diesen Link und melde dich an:</p>'\
                       f'<a href="{url}" style="color:#3b82f6">{url}</a>'\
                       f'<p style="margin:16px 0;color:#9ca3af">Nach der Anmeldung kopiere die URL aus der Adressleiste und gib sie im Hauptfenster ein.</p>'\
                       f'</body></html>'
        return "Auth URL nicht gefunden. Prüfe die Logs.", 500
    except Exception as e:
        return f"Fehler: {e}", 500

@app.route('/auth/complete', methods=['POST'])
def auth_complete():
    """Complete OAuth with response URL from user"""
    data = request.json
    response_url = data.get('code', '')
    try:
        # Write response URL to file for onedrive to read
        with open(f"{ONEDRIVE_CONFIG_DIR}/responseUrl", 'w') as f:
            f.write(response_url)
        # onedrive picks up the responseUrl file and writes refresh_token
        result = subprocess.run(
            ["onedrive", "--confdir", ONEDRIVE_CONFIG_DIR, "--auth-files", "authUrl:responseUrl"],
            capture_output=True, text=True, timeout=60
        )
        if os.path.exists(f"{ONEDRIVE_CONFIG_DIR}/refresh_token"):
            return jsonify({"success": True})
        return jsonify({"success": False, "error": result.stderr}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/config', methods=['POST'])
def update_config():
    """Merge incoming config changes into existing config"""
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
    """Trigger immediate sync in background"""
    def run_sync():
        subprocess.run(["python3", "/app/sync_manager.py"], timeout=300)
    threading.Thread(target=run_sync, daemon=True).start()
    return jsonify({"success": True})

if __name__ == '__main__':
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(ONEDRIVE_CONFIG_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=8765, debug=False)
