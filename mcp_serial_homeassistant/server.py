"""
MCP Serial HomeAssistant v1.0.4
Persistenter serieller Listener fuer ESP32-S2 USB-CDC.
FastMCP HTTP Transport.
Polling alle 0.5s (udev NETLINK in Docker geblockt).
ttyACM* hat immer Vorrang vor ttyUSB* (ESP32-S2 vs Zigbee-Dongles).
active_port wird NICHT auto-sticky gesetzt - nur wenn manuell per Tool konfiguriert.
"""
import logging
import os
import threading
import time
import glob
import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import serial
import uvicorn
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp_serial")
for _noisy in ("uvicorn.access", "uvicorn.error", "mcp", "fastmcp"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
def load_options() -> dict:
    try:
        with open("/data/options.json") as f:
            return json.load(f)
    except Exception:
        return {}

opts = load_options()

state = {
    "baud_rate":           opts.get("baud_rate", 115200),
    "active_port":         None,   # None = auto-detect; nur per MCP-Tool setzbar
    "ring_buffer_lines":   opts.get("ring_buffer_lines", 300),
    "log_retention_hours": opts.get("log_retention_hours", 24),
    "log_max_size_mb":     opts.get("log_max_size_mb", 20),
}
MCP_PORT = int(opts.get("port", 8769))

# ---------------------------------------------------------------------------
# Ring Buffer
# ---------------------------------------------------------------------------
ring_buffer: deque = deque(maxlen=state["ring_buffer_lines"])
buffer_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Disk Log
# ---------------------------------------------------------------------------
LOG_DIR = Path("/data/serial_logs")
LOG_DIR.mkdir(exist_ok=True)
current_log_file: Path | None = None
log_lock = threading.Lock()

def get_log_path() -> Path:
    return LOG_DIR / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + ".log")

def rotate_logs():
    cutoff = time.time() - state["log_retention_hours"] * 3600
    for f in LOG_DIR.glob("*.log"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except Exception:
            pass

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
        try:
            with open(current_log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.warning("Log write error: %s", e)

# ---------------------------------------------------------------------------
# Port-Erkennung
# ttyACM* hat immer Vorrang (ESP32-S2 USB-CDC).
# ttyUSB* nur als Fallback wenn kein ttyACM vorhanden.
# active_port wird NUR benutzt wenn manuell per serial_set_port gesetzt.
# ---------------------------------------------------------------------------
def find_target_port() -> str | None:
    target = state["active_port"]
    if target:
        # Manuell konfiguriert - nur zurueckgeben wenn vorhanden
        return target if os.path.exists(target) else None
    # Auto-detect: ttyACM bevorzugen (ESP32-S2), ttyUSB nur als Fallback
    acm = sorted(glob.glob("/dev/ttyACM*"))
    if acm:
        return acm[0]
    # Kein ttyACM -> kein ESP32-S2 verbunden, nichts oeffnen
    return None

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
                try:
                    ser.close()
                except Exception:
                    pass
            ser = s
        log.info("Port geoeffnet: %s @ %d baud", port, baud)
        return True
    except Exception as e:
        log.debug("Open fehlgeschlagen %s: %s", port, e)
        return False

def close_port():
    global ser
    with ser_lock:
        if ser:
            try:
                ser.close()
            except Exception:
                pass
            ser = None

# ---------------------------------------------------------------------------
# Haupt-Loop: Lesen + Polling
# ---------------------------------------------------------------------------
def serial_loop():
    last_seen_port = None

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
                log.info("Port verloren (ESP schlaeft) - polling...")
                close_port()
                last_seen_port = None
            except Exception as e:
                log.debug("Read error: %s", e)
                time.sleep(0.05)
        else:
            port = find_target_port()
            if port and port != last_seen_port:
                log.info("Port erkannt: %s - oeffne...", port)
                time.sleep(0.15)
                if open_port(port, state["baud_rate"]):
                    last_seen_port = port
                else:
                    last_seen_port = None
            elif not port:
                last_seen_port = None
            time.sleep(0.5)

# ---------------------------------------------------------------------------
# FastMCP Tools
# ---------------------------------------------------------------------------
mcp = FastMCP("MCP Serial HomeAssistant")

@mcp.tool()
def serial_read_recent(lines: int = 50) -> dict:
    """Letzte N Zeilen aus dem RAM-Ring-Buffer. Fuer schnelle Checks nach ESP32-S2 Aufwachen."""
    n = min(lines, state["ring_buffer_lines"])
    with buffer_lock:
        items = list(ring_buffer)[-n:]
    return {"lines": items, "count": len(items), "buffer_total": len(ring_buffer)}

@mcp.tool()
def serial_read_timerange(since: str = "", until: str = "", max_lines: int = 5000) -> dict:
    """Log-Eintraege aus Disk-Log fuer Zeitraum. since/until als ISO timestamp."""
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
    return {"lines": results, "count": len(results), "truncated": len(results) >= max_lines}

@mcp.tool()
def serial_list_ports() -> dict:
    """Zeigt alle verfuegbaren seriellen Ports (ttyACM*, ttyUSB*) inkl. by-id Symlinks."""
    ports = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    by_id = {}
    try:
        for name in os.listdir("/dev/serial/by-id"):
            target = os.readlink(f"/dev/serial/by-id/{name}")
            by_id[name] = os.path.normpath(os.path.join("/dev/serial/by-id", target))
    except Exception:
        pass
    return {"ports": ports, "by_id": by_id}

@mcp.tool()
def serial_set_port(port: str) -> dict:
    """Setzt den aktiven Port manuell (ueberschreibt Auto-Detect dauerhaft). Leer-String = zurueck zu Auto-Detect."""
    if port == "":
        state["active_port"] = None
        return {"active_port": None, "note": "Auto-Detect aktiv (ttyACM bevorzugt)"}
    state["active_port"] = port
    if not os.path.exists(port):
        return {"port": port, "opened": False, "note": "Existiert noch nicht, wird beim naechsten Aufwachen geoeffnet"}
    ok = open_port(port, state["baud_rate"])
    return {"port": port, "opened": ok}

@mcp.tool()
def serial_set_baudrate(baud_rate: int) -> dict:
    """Aendert die Baudrate live ohne Neustart. z.B. 115200, 9600, 921600"""
    state["baud_rate"] = baud_rate
    with ser_lock:
        s = ser
    if s and s.is_open:
        try:
            s.baudrate = baud_rate
            return {"baud_rate": baud_rate, "applied": True}
        except Exception as e:
            return {"baud_rate": baud_rate, "applied": False, "error": str(e)}
    return {"baud_rate": baud_rate, "applied": False, "note": "Kein Port offen, gilt beim naechsten Open"}

@mcp.tool()
def serial_status() -> dict:
    """Aktueller Status: Port, Baudrate, offen/zu, Buffer-Fuellstand, Log-Groesse."""
    with ser_lock:
        s = ser
    log_files = list(LOG_DIR.glob("*.log"))
    with buffer_lock:
        buf_len = len(ring_buffer)
    return {
        "port": s.port if s else None,
        "configured_port": state["active_port"],
        "auto_detect": state["active_port"] is None,
        "baud_rate": state["baud_rate"],
        "port_open": s.is_open if s else False,
        "ring_buffer_used": buf_len,
        "ring_buffer_capacity": state["ring_buffer_lines"],
        "log_files": len(log_files),
        "log_size_bytes": sum(f.stat().st_size for f in log_files),
    }

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=serial_loop, daemon=True).start()
    log.info("MCP Serial HomeAssistant v1.0.4 gestartet auf Port %d", MCP_PORT)
    log.info("Auto-Detect: ttyACM* bevorzugt (ESP32-S2), ttyUSB* ignoriert im Auto-Mode")
    app = mcp.http_app()
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="warning")
