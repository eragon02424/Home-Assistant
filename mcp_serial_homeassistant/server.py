#!/usr/bin/env python3
"""
MCP Serial HomeAssistant v1.0.0
Persistenter serieller Listener fuer ESP32-S2 USB-CDC.
pyudev Watcher: Port wird sofort nach USB-Enumeration geoeffnet.
Kein Byte geht verloren nach Deep Sleep.
"""
import asyncio
import json
import sys
import os
import threading
import time
import glob
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import serial
import pyudev

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
OPTIONS_FILE = "/data/options.json"
LOG_DIR = Path("/data/serial_logs")
LOG_DIR.mkdir(exist_ok=True)

def load_options():
    try:
        with open(OPTIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

opts = load_options()

state = {
    "baud_rate": opts.get("baud_rate", 115200),
    "active_port": None,
    "ring_buffer_lines": opts.get("ring_buffer_lines", 300),
    "log_retention_hours": opts.get("log_retention_hours", 24),
    "log_max_size_mb": opts.get("log_max_size_mb", 20),
}

# ---------------------------------------------------------------------------
# Ring Buffer
# ---------------------------------------------------------------------------
ring_buffer: deque = deque(maxlen=state["ring_buffer_lines"])
buffer_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Log-Datei
# ---------------------------------------------------------------------------
current_log_file: Path | None = None
log_lock = threading.Lock()

def get_log_path() -> Path:
    return LOG_DIR / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".log")

def rotate_logs():
    cutoff = time.time() - state["log_retention_hours"] * 3600
    for f in LOG_DIR.glob("*.log"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)

def write_log_line(entry: dict):
    global current_log_file
    with log_lock:
        rotate_logs()
        if current_log_file is None:
            current_log_file = get_log_path()
        try:
            if current_log_file.exists() and \
               current_log_file.stat().st_size > state["log_max_size_mb"] * 1024 * 1024:
                current_log_file = get_log_path()
        except Exception:
            current_log_file = get_log_path()
        with open(current_log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

# ---------------------------------------------------------------------------
# Auto-Detect
# ---------------------------------------------------------------------------
def auto_detect_port() -> str | None:
    candidates = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    return candidates[0] if candidates else None

# ---------------------------------------------------------------------------
# Serielle Verbindung
# ---------------------------------------------------------------------------
ser: serial.Serial | None = None
ser_lock = threading.Lock()

def open_port(port: str, baud: int) -> bool:
    global ser
    try:
        s = serial.Serial(port, baud, timeout=1, dsrdtr=False, rtscts=False)
        s.dtr = False
        s.rts = False
        with ser_lock:
            if ser and ser.is_open:
                ser.close()
            ser = s
        print(f"[serial] Geoeffnet: {port} @ {baud}", flush=True)
        return True
    except Exception as e:
        print(f"[serial] Open fehlgeschlagen {port}: {e}", flush=True)
        return False

def read_loop():
    while True:
        with ser_lock:
            s = ser
        if s and s.is_open:
            try:
                line = s.readline()
                if line:
                    decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    entry = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "port": s.port,
                        "line": decoded,
                    }
                    with buffer_lock:
                        ring_buffer.append(entry)
                    write_log_line(entry)
            except serial.SerialException:
                with ser_lock:
                    try:
                        ser.close()
                    except Exception:
                        pass
                time.sleep(0.05)
            except Exception:
                time.sleep(0.05)
        else:
            time.sleep(0.05)

# ---------------------------------------------------------------------------
# pyudev Watcher
# ---------------------------------------------------------------------------
def udev_watcher():
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="tty")
    for device in iter(monitor.poll, None):
        if device.action == "add":
            dev_node = device.device_node
            if dev_node and ("ttyACM" in dev_node or "ttyUSB" in dev_node):
                target = state["active_port"]
                if target is None or dev_node == target:
                    print(f"[udev] Neues Device: {dev_node} - oeffne...", flush=True)
                    time.sleep(0.1)
                    open_port(dev_node, state["baud_rate"])
                    if state["active_port"] is None:
                        state["active_port"] = dev_node

# ---------------------------------------------------------------------------
# MCP Tool Handler
# ---------------------------------------------------------------------------
def handle_serial_read_recent(params: dict) -> dict:
    n = min(int(params.get("lines", 50)), state["ring_buffer_lines"])
    with buffer_lock:
        items = list(ring_buffer)[-n:]
    return {"lines": items, "count": len(items), "buffer_total": len(ring_buffer)}

def handle_serial_read_timerange(params: dict) -> dict:
    since = params.get("since")
    until = params.get("until")
    max_lines = int(params.get("max_lines", 5000))
    results = []
    for lf in sorted(LOG_DIR.glob("*.log")):
        try:
            with open(lf) as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except Exception:
                        continue
                    ts = entry.get("ts", "")
                    if since and ts < since:
                        continue
                    if until and ts > until:
                        continue
                    results.append(entry)
                    if len(results) >= max_lines:
                        break
        except Exception:
            continue
        if len(results) >= max_lines:
            break
    return {
        "lines": results,
        "count": len(results),
        "since": since,
        "until": until,
        "truncated": len(results) >= max_lines,
    }

def handle_serial_list_ports(params: dict) -> dict:
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    by_id = {}
    try:
        for name in os.listdir("/dev/serial/by-id"):
            target = os.readlink(f"/dev/serial/by-id/{name}")
            by_id[name] = os.path.normpath(os.path.join("/dev/serial/by-id", target))
    except Exception:
        pass
    return {"ports": ports, "by_id": by_id}

def handle_serial_set_port(params: dict) -> dict:
    port = params.get("port")
    if not port:
        return {"error": "'port' Parameter fehlt"}
    if not os.path.exists(port):
        return {"error": f"{port} nicht gefunden"}
    state["active_port"] = port
    ok = open_port(port, state["baud_rate"])
    return {"port": port, "opened": ok}

def handle_serial_set_baudrate(params: dict) -> dict:
    baud = params.get("baud_rate")
    if not baud:
        return {"error": "'baud_rate' Parameter fehlt"}
    baud = int(baud)
    state["baud_rate"] = baud
    with ser_lock:
        s = ser
    if s and s.is_open:
        try:
            s.baudrate = baud
            return {"baud_rate": baud, "applied": True, "note": "Live geaendert"}
        except Exception as e:
            return {"baud_rate": baud, "applied": False, "error": str(e)}
    return {"baud_rate": baud, "applied": False, "note": "Kein Port offen, gilt beim naechsten Open"}

def handle_serial_status(params: dict) -> dict:
    with ser_lock:
        s = ser
    log_files = list(LOG_DIR.glob("*.log"))
    log_size = sum(f.stat().st_size for f in log_files)
    with buffer_lock:
        buf_len = len(ring_buffer)
    return {
        "port": s.port if s else None,
        "configured_port": state["active_port"],
        "baud_rate": state["baud_rate"],
        "port_open": s.is_open if s else False,
        "ring_buffer_used": buf_len,
        "ring_buffer_capacity": state["ring_buffer_lines"],
        "log_files": len(log_files),
        "log_size_bytes": log_size,
    }

# ---------------------------------------------------------------------------
# Tool-Definitionen
# ---------------------------------------------------------------------------
TOOLS = {
    "serial_read_recent":    handle_serial_read_recent,
    "serial_read_timerange": handle_serial_read_timerange,
    "serial_list_ports":     handle_serial_list_ports,
    "serial_set_port":       handle_serial_set_port,
    "serial_set_baudrate":   handle_serial_set_baudrate,
    "serial_status":         handle_serial_status,
}

TOOL_DEFS = [
    {
        "name": "serial_read_recent",
        "description": "Letzte N Zeilen aus dem RAM-Ring-Buffer (schnell, kein Disk-IO). Fuer kurze Checks nach ESP32-S2 Aufwachen.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "description": "Anzahl Zeilen (default 50, max 300)"}
            }
        }
    },
    {
        "name": "serial_read_timerange",
        "description": "Log-Eintraege aus persistierter Disk-Log-Datei fuer einen Zeitraum. Fuer groessere Analysen / 24h History.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Start ISO timestamp z.B. 2025-07-01T06:00:00+00:00"},
                "until": {"type": "string", "description": "Ende ISO timestamp (optional)"},
                "max_lines": {"type": "integer", "description": "Maximale Zeilen (default 5000)"}
            }
        }
    },
    {
        "name": "serial_list_ports",
        "description": "Zeigt alle verfuegbaren seriellen Ports auf dem HA-Host (ttyACM*, ttyUSB*) inkl. by-id Symlinks.",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "serial_set_port",
        "description": "Wechselt den aktiven seriellen Port live. Ueberschreibt Auto-Detect.",
        "inputSchema": {
            "type": "object",
            "required": ["port"],
            "properties": {
                "port": {"type": "string", "description": "Port-Pfad z.B. /dev/ttyACM0"}
            }
        }
    },
    {
        "name": "serial_set_baudrate",
        "description": "Aendert die Baudrate live ohne Neustart.",
        "inputSchema": {
            "type": "object",
            "required": ["baud_rate"],
            "properties": {
                "baud_rate": {"type": "integer", "description": "z.B. 115200, 9600, 921600"}
            }
        }
    },
    {
        "name": "serial_status",
        "description": "Aktueller Status: Port, Baudrate, offen/zu, Buffer-Fuellstand, Log-Groesse.",
        "inputSchema": {"type": "object", "properties": {}}
    },
]

# ---------------------------------------------------------------------------
# MCP stdio Loop
# ---------------------------------------------------------------------------
async def main():
    threading.Thread(target=read_loop, daemon=True).start()
    threading.Thread(target=udev_watcher, daemon=True).start()

    port = auto_detect_port()
    if port:
        state["active_port"] = port
        open_port(port, state["baud_rate"])
    else:
        print("[serial] Kein Port beim Start gefunden - warte auf udev...", flush=True)

    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    proto = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: proto, sys.stdin)

    async for raw in reader:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            continue

        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            resp = {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "mcp-serial-homeassistant", "version": "1.0.0"},
                    "capabilities": {"tools": {}}
                }
            }
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOL_DEFS}}
        elif method == "tools/call":
            tool_name = req["params"]["name"]
            tool_params = req["params"].get("arguments", {})
            if tool_name in TOOLS:
                try:
                    result = TOOLS[tool_name](tool_params)
                except Exception as e:
                    result = {"error": str(e)}
                resp = {
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
                }
            else:
                resp = {"jsonrpc": "2.0", "id": req_id,
                        "error": {"code": -32601, "message": f"Unbekanntes Tool: {tool_name}"}}
        else:
            resp = {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"Unbekannte Methode: {method}"}}

        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    asyncio.run(main())
